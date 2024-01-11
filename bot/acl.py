"""
Access Control Lists -- expression-based rules for allowing/disallowing actions.

An ACL is a formula that checks for roles, users, channels, and categories; and possibly combines these checks using
boolean algebra connectives. We have a configurable map from ACL names to these formulas.

For commands there's a decorator that marks the command as requiring permissions in general, and then we maintain a
configurable map from fully qualified command names to ACL names.

For other miscellaneous actions it is possible to register an action by name (so that the actions can be listed), and
then we maintain a configurable map from action names to ACL names.

Lastly each ACL can have a designated "meta"-ACL that controls when the permissions for the original ACL can be edited,
and when commands and actions bound to that original ACL can be bound to something else. Changing the
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
import enum
from functools import total_ordering
import logging
from typing import (Any, Awaitable, Callable, Dict, Iterator, List, Literal, Optional, Protocol, Set, Tuple, TypedDict,
    TypeVar, Union, cast, overload)

from discord import DMChannel, GroupChannel, Interaction, Member, Thread, User
from discord.abc import GuildChannel
import discord.ext.commands

from bot.commands import Context
import plugins
import util.db.kv
from util.discord import format

logger = logging.getLogger(__name__)

class RoleData(TypedDict):
    role: int

class UserData(TypedDict):
    user: int

class ChannelData(TypedDict):
    channel: int

class CategoryData(TypedDict):
    category: Optional[int]

NotData = TypedDict("NotData", {"not": "ACLData"})

AndData = TypedDict("AndData", {"and": List["ACLData"]})

OrData = TypedDict("OrData", {"or": List["ACLData"]})

class NestedData(TypedDict):
    acl: str

ACLData = Union[RoleData, UserData, ChannelData, CategoryData, NotData, AndData, OrData, NestedData]

class ACLConf(Awaitable[None], Protocol):
    @overload
    def __getitem__(self, key: Tuple[Literal["acl"], str]) -> Optional[ACLData]: ...

    @overload
    def __getitem__(self, key: Tuple[Literal["command"], str]) -> Optional[str]: ...

    @overload
    def __getitem__(self, key: Tuple[Literal["action"], str]) -> Optional[str]: ...

    @overload
    def __getitem__(self, key: Tuple[Literal["meta"], str]) -> Optional[str]: ...

    @overload
    def __setitem__(self, key: Tuple[Literal["acl"], str], value: Optional[ACLData]) -> None: ...

    @overload
    def __setitem__(self, key: Tuple[Literal["command"], str], value: Optional[str]) -> None: ...

    @overload
    def __setitem__(self, key: Tuple[Literal["action"], str], value: Optional[str]) -> None: ...

    @overload
    def __setitem__(self, key: Tuple[Literal["meta"], str], value: Optional[str]) -> None: ...

    def __iter__(self) -> Iterator[Tuple[Literal["acl", "comand", "action", "meta"], str]]: ...

@plugins.init
async def init() -> None:
    global conf
    conf = cast(ACLConf, await util.db.kv.load(__name__))

@total_ordering
class EvalResult(enum.Enum):
    FALSE = 0
    UNKNOWN = 1
    TRUE = 2

    def __lt__(self, other: EvalResult) -> bool:
        return self.value < other.value

MessageableChannel = Union[GuildChannel, Thread, DMChannel, GroupChannel]

class ACL(ABC):
    @abstractmethod
    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        raise NotImplemented

    @staticmethod
    def parse(data: ACLData) -> ACL:
        if "role" in data:
            return RoleACL(data["role"])
        elif "user" in data:
            return UserACL(data["user"])
        elif "channel" in data:
            return ChannelACL(data["channel"])
        elif "category" in data:
            return CategoryACL(data["category"])
        elif "not" in data:
            return NotACL(ACL.parse(data["not"]))
        elif "and" in data:
            return AndACL([ACL.parse(acl) for acl in data["and"]])
        elif "or" in data:
            return OrACL([ACL.parse(acl) for acl in data["or"]])
        elif "acl" in data:
            return NestedACL(data["acl"])
        raise ValueError("Invalid ACL data: {!r}".format(data))

    @abstractmethod
    def serialize(self) -> ACLData:
        raise NotImplemented

class RoleACL(ACL):
    role: int

    def __init__(self, role: int):
        self.role = role

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        if isinstance(user, Member):
            return EvalResult.TRUE if any(role.id == self.role for role in user.roles) else EvalResult.FALSE
        else:
            return EvalResult.UNKNOWN

    def serialize(self) -> ACLData:
        return {"role": self.role}

class UserACL(ACL):
    user: int

    def __init__(self, user: int):
        self.user = user

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        if user is not None:
            return EvalResult.TRUE if user.id == self.user else EvalResult.FALSE
        else:
            return EvalResult.UNKNOWN

    def serialize(self) -> ACLData:
        return {"user": self.user}


class ChannelACL(ACL):
    channel: int

    def __init__(self, channel: int):
        self.channel = channel

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        if channel is not None:
            if channel.id == self.channel or (isinstance(channel, Thread) and channel.parent_id == self.channel):
                return EvalResult.TRUE
            else:
                return EvalResult.FALSE
        else:
            return EvalResult.UNKNOWN

    def serialize(self) -> ACLData:
        return {"channel": self.channel}

class CategoryACL(ACL):
    category: Optional[int]

    def __init__(self, category: Optional[int]):
        self.category = category

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        if isinstance(channel, GuildChannel):
            if (channel.category.id if channel.category else None) == self.category:
                return EvalResult.TRUE
            else:
                return EvalResult.FALSE
        else:
            return EvalResult.UNKNOWN

    def serialize(self) -> ACLData:
        return {"category": self.category}

class NotACL(ACL):
    acl: ACL

    def __init__(self, acl: ACL):
        self.acl = acl

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        result = self.acl.evaluate(user, channel, nested)
        if result == EvalResult.FALSE:
            return EvalResult.TRUE
        elif result == EvalResult.TRUE:
            return EvalResult.FALSE
        else:
            return EvalResult.UNKNOWN

    def serialize(self) -> ACLData:
        return {"not": self.acl.serialize()}

class AndACL(ACL):
    acls: List[ACL]

    def __init__(self, acls: List[ACL]):
        self.acls = acls

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        return min((acl.evaluate(user, channel, nested) for acl in self.acls), default=EvalResult.TRUE)

    def serialize(self) -> ACLData:
        return {"and": [acl.serialize() for acl in self.acls]}

class OrACL(ACL):
    acls: List[ACL]

    def __init__(self, acls: List[ACL]):
        self.acls = acls

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        return max((acl.evaluate(user, channel, nested) for acl in self.acls), default=EvalResult.FALSE)

    def serialize(self) -> ACLData:
        return {"or": [acl.serialize() for acl in self.acls]}

class NestedACL(ACL):
    acl: str

    def __init__(self, acl: str):
        self.acl = acl

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
        ) -> EvalResult:
        return evaluate_acl(self.acl, user, channel, nested)

    def serialize(self) -> ACLData:
        return {"acl": self.acl}

def evaluate_acl(acl: Optional[str], user: Optional[Union[Member, User]], channel: Optional[MessageableChannel],
    nested: Set[str] = set()) -> EvalResult:
    """Given an ACL check whether the given user and channel satisfy it."""
    if acl is None:
        return EvalResult.UNKNOWN
    if acl in nested:
        return EvalResult.UNKNOWN
    if (data := conf["acl", acl]) is None:
        return EvalResult.UNKNOWN
    return ACL.parse(data).evaluate(user, channel, nested | {acl})

class ACLCheck:
    def __call__(self, ctx: Context) -> bool:
        assert ctx.command
        acl = conf["command", ctx.command.qualified_name]
        if max(acl_override.evaluate(*evaluate_ctx(ctx)), evaluate_acl(acl, *evaluate_ctx(ctx))) == EvalResult.TRUE:
            return True
        else:
            logger.warn(format("{!m}/{!c} did not match ACL {!r} for command {!r}",
                ctx.author, ctx.channel, acl, ctx.command.qualified_name))
            return False

CommandT = TypeVar("CommandT", bound=Callable[..., Any])

def privileged(fun: CommandT) -> CommandT:
    """A decorator indicating that a command should be bound an unspecified configurable ACL."""
    return discord.ext.commands.check(ACLCheck())(fun)

live_actions: Dict[str, int] = defaultdict(int)

class Action:
    action: str

    def __init__(self, action: str):
        self.action = action

    def evaluate(self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel],
        nested: Set[str] = set()) -> EvalResult:
        result = evaluate_acl(conf["action", self.action], user, channel, nested)
        if self != acl_override:
            result = max(result, acl_override.evaluate(user, channel))
        return result

def register_action(name: str) -> Action:
    """
    Must only be called during plugin initialization. Obtain a handle for a named action (that can be bound to a yet
    unspecified ACL)
    """
    def unregister_action():
        live_actions[name] -= 1
    plugins.finalizer(unregister_action)
    live_actions[name] += 1

    return Action(name)

acl_override = register_action("acl_override")

def evaluate_acl_meta(acl: Optional[str], user: Optional[Union[Member, User]], channel: Optional[MessageableChannel]
    ) -> EvalResult:
    """Given an ACL check whether the given user and channel can *edit* it."""
    result = acl_override.evaluate(user, channel)
    if acl is not None:
        result = max(result, evaluate_acl(conf["meta", acl], user, channel))
    return result

def evaluate_ctx(ctx: Context) -> Tuple[Union[Member, User], MessageableChannel]:
    return ctx.author, cast(MessageableChannel, ctx.channel)

def evaluate_interaction(interaction: Interaction) -> Tuple[Union[Member, User], Optional[MessageableChannel]]:
    return interaction.user, interaction.channel
