"""
Some common utilities for interacting with discord.
"""
from __future__ import annotations
import asyncio
import re
import math
import discord
import discord.abc
import discord.ext.commands
import string
import logging
from typing import (Any, List, Sequence, Callable, Iterable, Optional, Union, Coroutine, AsyncContextManager, Generic,
    TypeVar, Type, Protocol, cast)
import discord_client
import plugins

logger: logging.Logger = logging.getLogger(__name__)

def unsafe_hook_event(name: str, fun: Callable[..., Coroutine[Any, Any, None]]) -> None:
    if not asyncio.iscoroutinefunction(fun):
        raise TypeError("expected coroutine function")
    method_name = "on_" + name
    discord_client.client.add_listener(fun, name=method_name)

def unsafe_unhook_event(name: str, fun: Callable[..., Coroutine[Any, Any, None]]) -> None:
    method_name = "on_" + name
    discord_client.client.remove_listener(fun, name=method_name)

def event(name: str) -> Callable[
        [Callable[..., Coroutine[Any, Any, None]]],
        Callable[..., Coroutine[Any, Any, None]]]:
    """
    discord.py doesn't allow multiple functions to register for the same event.
    This decorator fixes that. Takes the event name without "on_" Example usage:

        @event("message")
        def func(msg):

    This function registers a finalizer that removes the registered function,
    and hence should only be called during plugin initialization.
    """
    def decorator(fun: Callable[..., Coroutine[Any, Any, None]]
        ) -> Callable[..., Coroutine[Any, Any, None]]:
        unsafe_hook_event(name, fun)
        @plugins.finalizer
        def finalizer() -> None:
            unsafe_unhook_event(name, fun)
        return fun
    return decorator

class Quoted:
    __slots__ = "text"
    text: str

    def __init__(self, text: str):
        self.text = text

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return "Quoted({!r})".format(self.text)

    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> Quoted:
        return cls(arg)

class CodeBlock(Quoted):
    __slots__ = "language"
    language: Optional[str]

    def __init__(self, text: str, language: Optional[str] = None):
        self.text = text
        self.language = language

    def __str__(self) -> str:
        text = self.text.replace("``", "`\u200D`")
        return "```{}\n".format(self.language or "") + text + "```"

    def __repr__(self) -> str:
        if self.language is None:
            return "CodeBlock({!r})".format(self.text)
        else:
            return "CodeBlock({!r}, language={!r})".format(self.text, self.language)

    codeblock_re: re.Pattern[str] = re.compile(r"```(?:(?P<language>\S*)\n(?!```))?(?P<block>(?:(?!```).)+)```", re.S)

    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> CodeBlock:
        if (match := cls.codeblock_re.match(ctx.view.buffer, pos=ctx.view.index - len(arg))) is not None: # type: ignore
            ctx.view.index = match.end() # type: ignore
            return cls(match["block"], match["language"] or None)
        raise discord.ext.commands.ArgumentParsingError("Please provide a codeblock")

class Inline(Quoted):
    __slots__ = "text"
    text: str

    def __init__(self, text: str):
        self.text = text

    def __str__(self) -> str:
        text = self.text
        if "`" in text:
            if "``" in text:
                text = text.replace("`", "`\u200D")
            if text.startswith("`"):
                text = " " + text
            if text.endswith("`"):
                text = text + " "
            return "``" + text + "``"
        return "`" + text + "`"

    def __repr__(self) -> str:
        return "Inline({!r})".format(self.text)

    inline_re: re.Pattern[str] = re.compile(r"``((?:(?!``).)+)``|`([^`]+)`", re.S)

    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> Inline:
        if (match := cls.inline_re.match(ctx.view.buffer, pos=ctx.view.index - len(arg))) is not None: # type: ignore
            ctx.view.index = match.end() # type: ignore
            return cls(match[1] or match[2])
        raise discord.ext.commands.ArgumentParsingError("Please provide an inline")

class Formatter(string.Formatter):
    """
    A formatter class designed for discord messages. The following conversions
    are understood:

        {!i} -- turn into inline code
        {!b} -- turn into a code block
        {!b:lang} -- turn into a code block in the specified language
        {!m} -- turn into mention
        {!M} -- turn into role mention
        {!c} -- turn into channel link
    """

    __slots__ = ()

    def convert_field(self, value: Any, conversion: str) -> Any:
        if conversion == "i":
            return str(Inline(str(value)))
        elif conversion == "b":
            return CodeBlock(str(value))
        elif conversion == "m":
            if isinstance(value, discord.Role):
                return "<@&{}>".format(value.id)
            elif isinstance(value, discord.abc.User):
                return "<@{}>".format(value.id)
            elif isinstance(value, int):
                return "<@{}>".format(value)
        elif conversion == "M":
            if isinstance(value, discord.Role):
                return "<@&{}>".format(value.id)
            elif isinstance(value, int):
                return "<@&{}>".format(value)
        elif conversion == "c":
            if isinstance(value, discord.TextChannel):
                return "<#{}>".format(value.id)
            elif isinstance(value, discord.CategoryChannel):
                return "<#{}>".format(value.id)
            elif isinstance(value, int):
                return "<#{}>".format(value)
        return super().convert_field(value, conversion)

    def format_field(self, value: Any, fmt: str) -> Any:
        if isinstance(value, CodeBlock):
            if fmt:
                value.language = fmt
            return str(value)
        return super().format_field(value, fmt)

formatter: string.Formatter = Formatter()
format = formatter.format

class UserError(discord.ext.commands.CommandError):
    __slots__ = ()

class InvocationError(discord.ext.commands.UserInputError):
    __slots__ = ()

class NamedType(Protocol):
    id: int
    name: str

class NicknamedType(Protocol):
    id: int
    name: str
    nick: str

M = TypeVar("M", bound=Union[NamedType, NicknamedType])

def smart_find(name_or_id: str, iterable: Iterable[M]) -> Optional[M]:
    """
    Find an object by its name or id. We try an exact id match, then the
    shortest prefix match, if unique among prefix matches of that length, then
    an infix match, if unique.
    """
    int_id: Optional[int]
    try:
        int_id = int(name_or_id)
    except ValueError:
        int_id = None
    prefix_match: Optional[M] = None
    prefix_matches: List[str] = []
    infix_matches: List[M] = []
    for x in iterable:
        if x.id == int_id:
            return x
        if x.name.startswith(name_or_id):
            if prefix_matches and len(x.name) < len(prefix_matches[0]):
                prefix_matches = []
            prefix_matches.append(x.name)
            prefix_match = x
        else:
            nick = getattr(x, "nick", None)
            if nick is not None and nick.startswith(name_or_id):
                if prefix_matches and len(nick) < len(prefix_matches[0]):
                    prefix_matches = []
                prefix_matches.append(nick)
                prefix_match = x
            elif name_or_id in x.name:
                infix_matches.append(x)
            elif nick is not None and name_or_id in nick:
                infix_matches.append(x)
    if len(prefix_matches) == 1:
        return prefix_match
    if len(infix_matches) == 1:
        return infix_matches[0]
    return None

T = TypeVar("T")

def priority_find(predicate: Callable[[T], Union[float, int, None]], iterable: Iterable[T]) -> List[T]:
    results = []
    cur_rank = None
    for x in iterable:
        rank = predicate(x)
        if rank is None:
            continue
        elif rank is math.inf:
            return [x]
        elif cur_rank is None or rank > cur_rank:
            cur_rank = rank
            results = [x]
        elif rank == cur_rank:
            results.append(x)
        elif rank < cur_rank:
            continue
    return results

class TempMessage(AsyncContextManager[discord.Message]):
    __slots__ = "sendable", "args", "kwargs", "message"
    sendable: discord.abc.Messageable
    args: Any
    kwargs: Any
    message: Optional[discord.Message]

    def __init__(self, sendable: discord.abc.Messageable,
        *args: Any, **kwargs: Any):
        self.sendable = sendable
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self) -> discord.Message:
        self.message = await self.sendable.send(*self.args, **self.kwargs)
        return self.message

    async def __aexit__(self, exc_type, exc_val, tb) -> None: # type: ignore
        try:
            if self.message is not None:
                await self.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

class ChannelById(discord.abc.Messageable):
    __slots__ = "id", "_state"
    id: int
    _state: discord.state.ConnectionState

    def __init__(self, client: discord.Client, id: int):
        self.id = id
        self._state = client._connection # type: ignore

    async def _get_channel(self) -> discord.abc.Messageable:
        return self

def nicknamed_priority(u: Union[NamedType, NicknamedType], s: str) -> Optional[int]:
    name = u.name
    nick = getattr(u, "nick", None)
    if s == name:
        return 3
    elif nick is not None and s == nick:
        return 3
    elif s.lower() == name.lower():
        return 2
    elif nick is not None and s.lower() == nick.lower():
        return 2
    elif name.lower().startswith(s.lower()):
        return 1
    elif nick is not None and nick.lower().startswith(s.lower()):
        return 1
    elif s.lower() in name.lower():
        return 0
    elif nick is not None and s.lower() in nick.lower():
        return 0
    else:
        return None

def named_priority(x: NamedType, s: str) -> Optional[int]:
    name = x.name
    if s == name:
        return 3
    elif s.lower() == name.lower():
        return 2
    elif name.lower().startswith(s.lower()):
        return 1
    elif s.lower() in name.lower():
        return 0
    else:
        return None

# We inherit XCoverter from X, so that given a declaration x: XConverter could be used with the assumption that really
# at runtime x: X
class PartialUserConverter(discord.abc.Snowflake):
    mention_re: re.Pattern[str] = re.compile(r"<@!?(\d+)>")
    id_re: re.Pattern[str] = re.compile(r"\d{15,}")
    discrim_re: re.Pattern[str] = re.compile(r"(.*)#(\d{4})")

    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> discord.abc.Snowflake:
        if match := cls.mention_re.fullmatch(arg):
            return discord.Object(int(match[1]))
        elif match := cls.id_re.fullmatch(arg):
            return discord.Object(int(match[0]))

        user_list: Sequence[Union[discord.User, discord.Member]]
        if ctx.guild is not None:
            user_list = ctx.guild.members
        else:
            user_list = [cast(discord.User, ctx.bot.user), ctx.author]
        if match := cls.discrim_re.fullmatch(arg):
            name, discrim = match[1], match[2]
            matches = list(filter(lambda u: u.name == name and u.discriminator == discrim, user_list))
            if len(matches) > 1:
                raise discord.ext.commands.BadArgument(format("Multiple results for {}#{}", name, discrim))
            elif len(matches) == 1:
                return matches[0]

        matches = priority_find(lambda u: nicknamed_priority(u, arg), user_list)
        if len(matches) > 1:
            raise discord.ext.commands.BadArgument(format("Multiple results for {}", arg))
        elif len(matches) == 1:
            return matches[0]
        else:
            raise discord.ext.commands.BadArgument(format("No results for {}", arg))

class MemberConverter(discord.User):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> Optional[discord.Member]:
        if ctx.guild is None:
            raise discord.ext.commands.NoPrivateMessage(format("Cannot obtain member outside guild"))

        obj = await PartialUserConverter.convert(ctx, arg)
        if isinstance(obj, discord.Member):
            return obj
        elif isinstance(obj, discord.User):
            raise discord.ext.commands.BadArgument(format("No member found by ID {}", obj.id))

        member = ctx.guild.get_member(obj.id)
        if member is not None: return member
        try:
            return await ctx.guild.fetch_member(obj.id)
        except discord.NotFound:
            raise discord.ext.commands.BadArgument(format("No member found by ID {}", obj.id))

class UserConverter(discord.User):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> Optional[discord.User]:
        obj = await PartialUserConverter.convert(ctx, arg)
        if isinstance(obj, discord.User):
            return obj
        user = ctx.bot.get_user(obj.id)

        if user is not None: return user
        try:
            return await ctx.bot.fetch_user(obj.id)
        except discord.NotFound:
            raise discord.ext.commands.BadArgument(format("No user found by ID {}", obj.id))

class PartialRoleConverter(discord.abc.Snowflake):
    mention_re: re.Pattern[str] = re.compile(r"<@&(\d+)>")
    id_re: re.Pattern[str] = re.compile(r"\d{15,}")

    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> discord.abc.Snowflake:
        if match := cls.mention_re.fullmatch(arg):
            return discord.Object(int(match[1]))
        elif match := cls.id_re.fullmatch(arg):
            return discord.Object(int(match[0]))

        if ctx.guild is None:
            raise discord.ext.commands.NoPrivateMessage(format("Outside a guild a role can only be specified by ID"))

        matches = priority_find(lambda r: named_priority(r, arg), ctx.guild.roles)
        if len(matches) > 1:
            raise discord.ext.commands.BadArgument(format("Multiple results for {}", arg))
        elif len(matches) == 1:
            return matches[0]
        else:
            raise discord.ext.commands.BadArgument(format("No results for {}", arg))

class RoleConverter(discord.Role):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> discord.Role:
        obj = await PartialRoleConverter.convert(ctx, arg)
        if isinstance(obj, discord.Role):
            return obj
        if ctx.guild is not None:
            role = ctx.guild.get_role(obj.id)
            if role is not None:
                return role
        for guild in ctx.bot.guilds:
            role = guild.get_role(obj.id)
            if role is not None:
                return role
        else:
            raise discord.ext.commands.BadArgument(format("No role found by ID {}", obj.id))

C = TypeVar("C", bound=discord.abc.GuildChannel)

class PCConv(Generic[C]):
    mention_re: re.Pattern[str] = re.compile(r"<#(\d+)>")
    id_re: re.Pattern[str] = re.compile(r"\d{15,}")

    @classmethod
    async def partial_convert(cls, ctx: discord.ext.commands.Context, arg: str, ty: Type[C]) -> discord.abc.Snowflake:
        if match := cls.mention_re.fullmatch(arg):
            return discord.Object(int(match[1]))
        elif match := cls.id_re.fullmatch(arg):
            return discord.Object(int(match[0]))

        if ctx.guild is None:
            raise discord.ext.commands.NoPrivateMessage(format("Outside a guild a channel can only be specified by ID"))

        chan_list: Sequence[discord.abc.GuildChannel] = ctx.guild.channels
        if ty == discord.TextChannel:
            chan_list = ctx.guild.text_channels
        elif ty == discord.VoiceChannel:
            chan_list = ctx.guild.voice_channels
        elif ty == discord.CategoryChannel:
            chan_list = ctx.guild.categories
        elif ty == discord.StageChannel:
            chan_list = ctx.guild.stage_channels

        matches = priority_find(lambda c: named_priority(c, arg), chan_list)
        if len(matches) > 1:
            raise discord.ext.commands.BadArgument(format("Multiple results for {}", arg))
        elif len(matches) == 1:
            return matches[0]
        else:
            raise discord.ext.commands.BadArgument(format("No results {}", arg))

    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str, ty: Type[C]) -> C:
        obj = await cls.partial_convert(ctx, arg, ty)
        if isinstance(obj, ty):
            return obj
        if ctx.guild is not None:
            chan = ctx.guild.get_channel(obj.id)
            if chan is not None:
                if not isinstance(chan, ty):
                    raise discord.ext.commands.BadArgument(format("{!c} is not a {}", chan.id, ty))
                return chan
        for guild in ctx.bot.guilds:
            chan = guild.get_channel(obj.id)
            if chan is not None:
                if not isinstance(chan, ty):
                    raise discord.ext.commands.BadArgument(format("{!c} is not a {}", chan.id, ty))
                return chan
        else:
            raise discord.ext.commands.BadArgument(format("No {} found by ID {}", obj.id))

class PartialChannelConverter(discord.abc.GuildChannel):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> discord.abc.Snowflake:
        return await PCConv.partial_convert(ctx, arg, discord.abc.GuildChannel)

class PartialTextChannelConverter(discord.abc.GuildChannel):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> discord.abc.Snowflake:
        return await PCConv.partial_convert(ctx, arg, discord.TextChannel)

class PartialCategoryChannelConverter(discord.abc.GuildChannel):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> discord.abc.Snowflake:
        return await PCConv.partial_convert(ctx, arg, discord.CategoryChannel)

class ChannelConverter(discord.abc.GuildChannel):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> discord.abc.GuildChannel:
        return await PCConv.convert(ctx, arg, discord.abc.GuildChannel)
