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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple, TypedDict, TypeVar, Union, cast

from discord import DMChannel, GroupChannel, Interaction, Member, Thread, User
from discord.abc import GuildChannel
import discord.ext.commands
from sqlalchemy import TEXT, ForeignKey, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

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

registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)


@registry.mapped
class ACL:
    __tablename__ = "acls"
    __table_args__ = {"schema": "permissions"}

    name: Mapped[str] = mapped_column(TEXT, primary_key=True)
    data: Mapped[ACLData] = mapped_column(JSONB, nullable=False)
    meta: Mapped[Optional[str]] = mapped_column(TEXT, ForeignKey(name))

    @staticmethod
    def parse_data(data: ACLData) -> ACLExpr:
        if "role" in data:
            return RoleACL(data["role"])
        elif "user" in data:
            return UserACL(data["user"])
        elif "channel" in data:
            return ChannelACL(data["channel"])
        elif "category" in data:
            return CategoryACL(data["category"])
        elif "not" in data:
            return NotACL(ACL.parse_data(data["not"]))
        elif "and" in data:
            return AndACL([ACL.parse_data(acl) for acl in data["and"]])
        elif "or" in data:
            return OrACL([ACL.parse_data(acl) for acl in data["or"]])
        elif "acl" in data:
            return NestedACL(data["acl"])
        raise ValueError("Invalid ACL data: {!r}".format(data))

    def parse(self) -> ACLExpr:
        return ACL.parse_data(self.data)

    if TYPE_CHECKING:

        def __init__(self, *, name: str, data: ACLData, meta: Optional[str] = ...) -> None:
            ...


@registry.mapped
class CommandPermissions:
    __tablename__ = "commands"
    __table_args__ = {"schema": "permissions"}

    name: Mapped[str] = mapped_column(TEXT, primary_key=True)
    acl: Mapped[str] = mapped_column(TEXT, ForeignKey(ACL.name), nullable=False)

    if TYPE_CHECKING:

        def __init__(self, *, name: str, acl: str) -> None:
            ...


@registry.mapped
class ActionPermissions:
    __tablename__ = "actions"
    __table_args__ = {"schema": "permissions"}

    name: Mapped[str] = mapped_column(TEXT, primary_key=True)
    acl: Mapped[str] = mapped_column(TEXT, ForeignKey(ACL.name), nullable=False)

    if TYPE_CHECKING:

        def __init__(self, *, name: str, acl: str) -> None:
            ...


acls: Dict[str, ACL]
commands: Dict[str, str]
actions: Dict[str, str]


@plugins.init
async def init() -> None:
    global acls, commands, actions
    await util.db.init(util.db.get_ddl(CreateSchema("permissions"), registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        for kind, name in conf:
            if kind == "acl":
                session.add(ACL(name=name, data=ACL.parse_data(cast(ACLData, conf["acl", name])).serialize()))
            elif kind == "command":
                session.add(CommandPermissions(name=name, acl=cast(str, conf["command", name])))
            elif kind == "action":
                session.add(ActionPermissions(name=name, acl=cast(str, conf["action", name])))
        await session.commit()
        for kind, name in conf:
            if kind == "meta":
                acl = await session.get(ACL, name)
                assert acl
                acl.meta = cast(str, conf["meta", name])
        await session.commit()
        for kind, name in [(kind, name) for kind, name in conf]:
            conf[kind, name] = None
        await conf

        stmt = select(ACL)
        acls = {acl.name: acl for acl in (await session.execute(stmt)).scalars()}
        stmt = select(CommandPermissions)
        commands = {command.name: command.acl for command in (await session.execute(stmt)).scalars()}
        stmt = select(ActionPermissions)
        actions = {command.name: command.acl for command in (await session.execute(stmt)).scalars()}


@total_ordering
class EvalResult(enum.Enum):
    FALSE = 0
    UNKNOWN = 1
    TRUE = 2

    def __lt__(self, other: EvalResult) -> bool:
        return self.value < other.value


MessageableChannel = Union[GuildChannel, Thread, DMChannel, GroupChannel]


class ACLExpr(ABC):
    @abstractmethod
    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
    ) -> EvalResult:
        raise NotImplemented

    @abstractmethod
    def serialize(self) -> ACLData:
        raise NotImplemented


class RoleACL(ACLExpr):
    role: int

    def __init__(self, role: int):
        self.role = role

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
    ) -> EvalResult:
        if isinstance(user, Member):
            return EvalResult.TRUE if any(role.id == self.role for role in user.roles) else EvalResult.FALSE
        else:
            return EvalResult.UNKNOWN

    def serialize(self) -> ACLData:
        return {"role": self.role}


class UserACL(ACLExpr):
    user: int

    def __init__(self, user: int):
        self.user = user

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
    ) -> EvalResult:
        if user is not None:
            return EvalResult.TRUE if user.id == self.user else EvalResult.FALSE
        else:
            return EvalResult.UNKNOWN

    def serialize(self) -> ACLData:
        return {"user": self.user}


class ChannelACL(ACLExpr):
    channel: int

    def __init__(self, channel: int):
        self.channel = channel

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
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


class CategoryACL(ACLExpr):
    category: Optional[int]

    def __init__(self, category: Optional[int]):
        self.category = category

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
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


class NotACL(ACLExpr):
    acl: ACLExpr

    def __init__(self, acl: ACLExpr):
        self.acl = acl

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
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


class AndACL(ACLExpr):
    acls: List[ACLExpr]

    def __init__(self, acls: List[ACLExpr]):
        self.acls = acls

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
    ) -> EvalResult:
        return min((acl.evaluate(user, channel, nested) for acl in self.acls), default=EvalResult.TRUE)

    def serialize(self) -> ACLData:
        return {"and": [acl.serialize() for acl in self.acls]}


class OrACL(ACLExpr):
    acls: List[ACLExpr]

    def __init__(self, acls: List[ACLExpr]):
        self.acls = acls

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
    ) -> EvalResult:
        return max((acl.evaluate(user, channel, nested) for acl in self.acls), default=EvalResult.FALSE)

    def serialize(self) -> ACLData:
        return {"or": [acl.serialize() for acl in self.acls]}


class NestedACL(ACLExpr):
    acl: str

    def __init__(self, acl: str):
        self.acl = acl

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str]
    ) -> EvalResult:
        return evaluate_acl(self.acl, user, channel, nested)

    def serialize(self) -> ACLData:
        return {"acl": self.acl}


def evaluate_acl(
    acl: Optional[str],
    user: Optional[Union[Member, User]],
    channel: Optional[MessageableChannel],
    nested: Set[str] = set(),
) -> EvalResult:
    """Given an ACL check whether the given user and channel satisfy it."""
    if acl is None:
        return EvalResult.UNKNOWN
    if acl in nested:
        return EvalResult.UNKNOWN
    if (data := acls.get(acl)) is None:
        return EvalResult.UNKNOWN
    return data.parse().evaluate(user, channel, nested | {acl})


class ACLCheck:
    def __call__(self, ctx: Context) -> bool:
        assert ctx.command
        acl = commands.get(ctx.command.qualified_name)
        if max(acl_override.evaluate(*evaluate_ctx(ctx)), evaluate_acl(acl, *evaluate_ctx(ctx))) == EvalResult.TRUE:
            return True
        else:
            logger.warn(
                format(
                    "{!m}/{!c} did not match ACL {!r} for command {!r}",
                    ctx.author,
                    ctx.channel,
                    acl,
                    ctx.command.qualified_name,
                )
            )
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

    def evaluate(
        self, user: Optional[Union[Member, User]], channel: Optional[MessageableChannel], nested: Set[str] = set()
    ) -> EvalResult:
        result = evaluate_acl(actions.get(self.action), user, channel, nested)
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


def evaluate_acl_meta(
    acl: Optional[str], user: Optional[Union[Member, User]], channel: Optional[MessageableChannel]
) -> EvalResult:
    """Given an ACL check whether the given user and channel can *edit* it."""
    result = acl_override.evaluate(user, channel)
    if acl is not None:
        if (data := acls.get(acl)) is not None:
            meta = data.meta
        else:
            meta = None
        result = max(result, evaluate_acl(meta, user, channel))
    return result


def evaluate_ctx(ctx: Context) -> Tuple[Union[Member, User], MessageableChannel]:
    return ctx.author, cast(MessageableChannel, ctx.channel)


def evaluate_interaction(interaction: Interaction) -> Tuple[Union[Member, User], Optional[MessageableChannel]]:
    return interaction.user, interaction.channel
