"""
Some common utilities for interacting with discord.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from io import BytesIO
import logging
import math
import re
import string
from typing import (Any, AsyncContextManager, Callable, Dict, Generic, Iterable, Iterator, List, Optional, Protocol,
    Sequence, Tuple, Type, TypeVar, Union, cast)

import discord
from discord import (CategoryChannel, File, ForumChannel, Member, Message, Object, PartialMessage, Role, StageChannel,
    TextChannel, User, VoiceChannel)
from discord.abc import GuildChannel, Messageable, Snowflake
import discord.context_managers
from discord.ext.commands import (ArgumentParsingError, BadArgument, CommandError, Context, MessageNotFound,
    NoPrivateMessage, PartialMessageConverter, UserInputError)
import discord.ext.commands.view
from discord.ext.commands.view import StringView
import discord.state

from bot.client import client

logger: logging.Logger = logging.getLogger(__name__)

class Quoted:
    """This class is a command argument converter equivalent to the behavior of a str argument."""
    __slots__ = "text"
    text: str

    def __init__(self, text: str):
        self.text = text

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return "Quoted({!r})".format(self.text)

    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Quoted:
        return cls(arg)

def undo_get_quoted_word(view: StringView, arg: str) -> int:
    """
    When converting command arguments, discord.py calls StringView.get_quoted_word to extract either a word or a quoted
    string, and passes that as an argument to the converter. This function does its best to undo that effect so that
    a converter can possibly handle the quotes differently.
    """
    escaped_quotes: Iterable[str] = discord.ext.commands.view._all_quotes
    offset = 0
    last = view.buffer[view.index - 1]
    if last == "\\":
        offset = 1
    elif not arg.endswith(last):
        for open_quote, close_quote in discord.ext.commands.view._quotes.items():
            if close_quote == last:
                escaped_quotes = (open_quote, close_quote)
                offset = 2
                break
    return view.index - offset - len(arg) - sum(ch in escaped_quotes for ch in arg)

class CodeBlock(Quoted):
    """A command argument in Discord's ```code block``` syntax"""

    __slots__ = "language"
    language: Optional[str]

    def __init__(self, text: str, *, language: Optional[str] = None):
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
    async def convert(cls, ctx: Context[Any], arg: str) -> CodeBlock:
        if (match := cls.codeblock_re.match(ctx.view.buffer, pos=undo_get_quoted_word(ctx.view, arg))) is not None:
            ctx.view.index = match.end()
            return cls(match["block"], language=match["language"] or None)
        raise ArgumentParsingError("Please provide a codeblock")

class Inline(Quoted):
    """A command argument in Discord's `inline code` syntax."""
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
    async def convert(cls, ctx: Context[Any], arg: str) -> Inline:
        if (match := cls.inline_re.match(ctx.view.buffer, pos=undo_get_quoted_word(ctx.view, arg))) is not None:
            ctx.view.index = match.end()
            return cls(match[1] or match[2])
        raise ArgumentParsingError("Please provide an inline")

class Formatter(string.Formatter):
    """
    A formatter class designed for discord messages. The following conversions
    are understood:

        {!i} -- turn a str-convertible value into inline code
        {!b} -- turn a str-convertible value into a code block
        {!b:lang} -- turn into a code block in the specified language
        {!m} -- turn an int or a discord object into a mention (defaults to user mention, unless a Role is provided)
        {!M} -- turn an int or a discord object into role mention
        {!c} -- turn an int or a discord object into channel link
    """

    __slots__ = ()

    def convert_field(self, value: Any, conversion: str) -> Any:
        if conversion == "i":
            return str(Inline(str(value)))
        elif conversion == "b":
            return CodeBlock(str(value))
        elif conversion == "m":
            if isinstance(value, Role):
                return "<@&{}>".format(value.id)
            elif isinstance(value, Snowflake):
                return "<@{}>".format(value.id)
            elif isinstance(value, int):
                return "<@{}>".format(value)
        elif conversion == "M":
            if isinstance(value, Role):
                return "<@&{}>".format(value.id)
            elif isinstance(value, Snowflake):
                return "<@&{}>".format(value.id)
            elif isinstance(value, int):
                return "<@&{}>".format(value)
        elif conversion == "c":
            if isinstance(value, GuildChannel):
                return "<#{}>".format(value.id)
            elif isinstance(value, Snowflake):
                return "<@{}>".format(value.id)
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

class UserError(CommandError):
    """General exceptions in commands."""
    __slots__ = ()

class InvocationError(UserInputError):
    """Exceptions in commands that are to do with the user input. Triggers displaying the command's usage."""
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
    """
    Finds those results in the input for which the predicate returns the highest rank, ignoring those for which the rank
    is None, and if any item has rank math.inf, the first such item is returned.
    """
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

class Typing(AsyncContextManager[None]):
    """An async context manager that starts a typing indication after a short timeout."""
    __slots__ = "typing", "timeout", "lock", "task"
    typing: discord.context_managers.Typing
    timeout: float
    lock: asyncio.Lock
    task: Optional[asyncio.Task[None]]

    def __init__(self, sendable: Messageable, *, timeout: float = 1.0):
        self.typing = sendable.typing()
        self.timeout = timeout
        self.lock = asyncio.Lock()

    async def start_typing(self) -> None:
        await asyncio.sleep(self.timeout)
        async with self.lock:
            self.task = None
            await self.typing.__aenter__()

    async def __aenter__(self) -> None:
        self.task = asyncio.create_task(self.start_typing(), name="Typing")

    async def __aexit__(self, exc_type, exc_val, tb) -> None: # type: ignore
        async with self.lock:
            if self.task:
                self.task.cancel()
            else:
                await self.typing.__aexit__(exc_type, exc_val, tb)

class TempMessage(AsyncContextManager[Message]):
    """An async context manager that sends a message upon entering, and deletes it upon exiting."""
    __slots__ = "sendable", "args", "kwargs", "message"
    sendable: Messageable
    args: Any
    kwargs: Any
    message: Optional[Message]

    def __init__(self, sendable: Messageable, *args: Any, **kwargs: Any):
        self.sendable = sendable
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self) -> Message:
        self.message = await self.sendable.send(*self.args, **self.kwargs)
        return self.message

    async def __aexit__(self, exc_type, exc_val, tb) -> None: # type: ignore
        try:
            if self.message is not None:
                await self.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

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

# Argument converters for various Discord datatypes
# We inherit XCoverter from X, so that given a declaration x: XConverter could be used with the assumption that really
# at runtime x: X
class PartialUserConverter(Snowflake):
    id: int
    mention_re: re.Pattern[str] = re.compile(r"<@!?(\d+)>")
    id_re: re.Pattern[str] = re.compile(r"\d{15,}")
    discrim_re: re.Pattern[str] = re.compile(r"(.*)#(\d{4})")

    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Snowflake:
        if match := cls.mention_re.fullmatch(arg):
            return Object(int(match[1]))
        elif match := cls.id_re.fullmatch(arg):
            return Object(int(match[0]))

        user_list: Sequence[Union[User, Member]]
        if ctx.guild is not None:
            user_list = ctx.guild.members
            where = "on this server"
        else:
            user_list = [cast(User, ctx.bot.user), ctx.author]
            where = "in this DM"
        if match := cls.discrim_re.fullmatch(arg):
            name, discrim = match[1], match[2]
            matches = list(filter(lambda u: u.name == name and u.discriminator == discrim, user_list))
            if len(matches) > 1:
                raise BadArgument(format("Multiple users match {}#{} {}", name, discrim, where))
            elif len(matches) == 1:
                return matches[0]

        matches = priority_find(lambda u: nicknamed_priority(u, arg), user_list)
        if len(matches) > 1:
            raise BadArgument(format("Multiple users match {} {}", arg, where))
        elif len(matches) == 1:
            return matches[0]
        else:
            raise BadArgument(format("Could not find user {} {}", arg, where))

class MemberConverter(User):
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Optional[Member]:
        if ctx.guild is None:
            raise NoPrivateMessage(format("Cannot obtain member outside a server"))

        obj = await PartialUserConverter.convert(ctx, arg)
        if isinstance(obj, Member):
            return obj
        elif isinstance(obj, User):
            raise BadArgument(format("Found a user with ID {}, but they are not on this server", obj.id))

        member = ctx.guild.get_member(obj.id)
        if member is not None: return member
        try:
            return await ctx.guild.fetch_member(obj.id)
        except discord.NotFound:
            raise BadArgument(format("Could not find member with ID {} on this server", obj.id))

class UserConverter(User):
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Optional[User]:
        obj = await PartialUserConverter.convert(ctx, arg)
        if isinstance(obj, User):
            return obj
        user = ctx.bot.get_user(obj.id)

        if user is not None: return user
        try:
            return await ctx.bot.fetch_user(obj.id)
        except discord.NotFound:
            raise BadArgument(format("Could not find user with ID {}", obj.id))

class PartialRoleConverter(Snowflake):
    id: int
    mention_re: re.Pattern[str] = re.compile(r"<@&(\d+)>")
    id_re: re.Pattern[str] = re.compile(r"\d{15,}")

    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Snowflake:
        if match := cls.mention_re.fullmatch(arg):
            return Object(int(match[1]))
        elif match := cls.id_re.fullmatch(arg):
            return Object(int(match[0]))

        if ctx.guild is None:
            raise NoPrivateMessage(format("Outside a server a role can only be specified by ID"))

        matches = priority_find(lambda r: named_priority(r, arg), ctx.guild.roles)
        if len(matches) > 1:
            raise BadArgument(format("Multiple roles match {} on this server", arg))
        elif len(matches) == 1:
            return matches[0]
        else:
            raise BadArgument(format("Could not find role {} on this server", arg))

class RoleConverter(Role):
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Role:
        obj = await PartialRoleConverter.convert(ctx, arg)
        if isinstance(obj, Role):
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
            raise BadArgument(format("Could not find role with ID {} in any server", obj.id))

C = TypeVar("C", bound=GuildChannel)

class PCConv(Generic[C]):
    mention_re: re.Pattern[str] = re.compile(r"<#(\d+)>")
    id_re: re.Pattern[str] = re.compile(r"\d{15,}")
    kind_map: Dict[Type[GuildChannel], str] = {
        GuildChannel: "channel",
        TextChannel: "text channel",
        VoiceChannel: "voice channel",
        CategoryChannel: "category channel",
        StageChannel: "stage channel",
        ForumChannel: "forum"}


    @classmethod
    async def partial_convert(cls, ctx: Context[Any], arg: str, ty: Type[C]) -> Snowflake:
        if match := cls.mention_re.fullmatch(arg):
            return Object(int(match[1]))
        elif match := cls.id_re.fullmatch(arg):
            return Object(int(match[0]))

        if ctx.guild is None:
            raise NoPrivateMessage(format("Outside a server a channel can only be specified by ID"))

        chan_list: Sequence[GuildChannel] = ctx.guild.channels
        if ty == TextChannel:
            chan_list = ctx.guild.text_channels
        elif ty == VoiceChannel:
            chan_list = ctx.guild.voice_channels
        elif ty == CategoryChannel:
            chan_list = ctx.guild.categories
        elif ty == StageChannel:
            chan_list = ctx.guild.stage_channels
        elif ty == ForumChannel:
            chan_list = ctx.guild.forums
        kind = cls.kind_map.get(ty, "channel")

        matches = priority_find(lambda c: named_priority(c, arg), chan_list)
        if len(matches) > 1:
            raise BadArgument(format("Multiple {}s match {} on this server", kind, arg))
        elif len(matches) == 1:
            return matches[0]
        else:
            raise BadArgument(format("Could not find {} {} on this server", kind, arg))

    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str, ty: Type[C]) -> C:
        obj = await cls.partial_convert(ctx, arg, ty)
        if isinstance(obj, ty):
            return obj
        kind = cls.kind_map.get(ty, "channel")
        if ctx.guild is not None:
            chan = ctx.guild.get_channel(obj.id)
            if chan is not None:
                if not isinstance(chan, ty):
                    raise BadArgument(format("{!c} is not a {}", chan.id, kind))
                return chan
        for guild in ctx.bot.guilds:
            chan = guild.get_channel(obj.id)
            if chan is not None:
                if not isinstance(chan, ty):
                    raise BadArgument(format("{!c} is not a {}", chan.id, kind))
                return chan
        else:
            raise BadArgument(format("Could not find {} by ID {} on any server", kind, obj.id))

class PartialChannelConverter(GuildChannel):
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Snowflake:
        return await PCConv.partial_convert(ctx, arg, GuildChannel)

class PartialTextChannelConverter(GuildChannel):
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Snowflake:
        return await PCConv.partial_convert(ctx, arg, TextChannel)

class PartialCategoryChannelConverter(GuildChannel):
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> Snowflake:
        return await PCConv.partial_convert(ctx, arg, CategoryChannel)

class ChannelConverter(GuildChannel):
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> GuildChannel:
        return await PCConv.convert(ctx, arg, GuildChannel)

def partial_message(channel: Snowflake, id: int) -> PartialMessage:
    return PartialMessage(channel=client.get_partial_messageable(channel.id), id=id)

def partial_from_reply(pmsg: Optional[PartialMessage], ctx: Context[Any]) -> PartialMessage:
    if pmsg is not None:
        return pmsg
    if (ref := ctx.message.reference) is not None:
        if isinstance(msg := ref.resolved, Message):
            return partial_message(msg.channel, msg.id)
        if (channel := client.get_channel(ref.channel_id)) is None:
            raise InvocationError(format("Could not find channel by ID {}", ref.channel_id))
        if ref.message_id is None:
            raise InvocationError("Referenced message has no ID")
        return partial_message(channel, ref.message_id)
    raise InvocationError("Expected either a message link, channel ID - message ID, or a reply to a message")

class ReplyConverter(PartialMessage):
    """
    Parse a PartialMessage either from either the replied-to message, or from the command (using an URL or a
    ChannelID-MessageID). If the command ends before this argument is parsed, the converter won't even be called, so if
    this is the last non-optional parameter, wrap it in Optional, and pass the result via partial_from_reply.
    """
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> PartialMessage:
        pos = undo_get_quoted_word(ctx.view, arg)
        if ctx.message.reference is not None:
            ctx.view.index = pos
            return partial_from_reply(None, ctx)
        try:
            return await PartialMessageConverter().convert(ctx, arg)
        except MessageNotFound:
            raise BadArgument("Expected either a message link or channel ID - message ID")

class DurationConverter(timedelta):
    time_re = re.compile(
        r"""
        \s*(-?\d+)\s*(?:
        (?P<seconds> s(?:ec(?:ond)?s?)?) |
        (?P<minutes> min(?:ute)?s? | (?!mo)(?-i:m)) |
        (?P<hours> h(?:(?:ou)?rs?)?) |
        (?P<days> d(?:ays?)?) |
        (?P<weeks> w(?:(?:ee)?ks?)?) |
        (?P<months> months? | (?-i:M)) |
        (?P<years> y(?:(?:ea)?rs?)?))\W*
        """,
        re.VERBOSE | re.IGNORECASE
    )
    time_expansion = {
        "seconds": timedelta(seconds=1),
        "minutes": timedelta(minutes=1),
        "hours": timedelta(hours=1),
        "days": timedelta(days=1),
        "weeks": timedelta(days=7),
        "months": timedelta(days=30),
        "years": timedelta(days=365)
    }
    @classmethod
    async def convert(cls, ctx: Context[Any], arg: str) -> timedelta:
        pos = undo_get_quoted_word(ctx.view, arg)
        td = timedelta()
        while (match := cls.time_re.match(ctx.view.buffer, pos=pos)) is not None:
            pos = match.end()
            assert match.lastgroup is not None
            td += int(match[1]) * cls.time_expansion[match.lastgroup]
        if pos == undo_get_quoted_word(ctx.view, arg):
            raise BadArgument("Expected a duration")
        ctx.view.index = pos
        return td

class PlainItem:
    """An item that is formatted as itself, possibly split across multiple messages if too large."""
    __slots__ = "text",
    text: str
    def __init__(self, text: str):
        self.text = text

class CodeItem:
    """An item that is formatted as either a code block, or an attached file if too large."""
    __slots__ = "text", "language", "filename"
    text: str
    language: Optional[str]
    filename: Optional[str]
    def __init__(self, text: str, *, language: Optional[str] = None, filename: Optional[str] = None):
        self.text = text
        self.language = language
        self.filename = filename


def chunk_messages(items: Iterable[Union[PlainItem, CodeItem]]) -> Iterator[Tuple[str, List[File]]]:
    """Format a sequence of items fitting as many as possible into each message."""
    MAX_CONTENT = 2000
    MAX_FILES = 10
    content, files = "", []
    files = []
    for item in items:
        if isinstance(item, PlainItem):
            text = item.text
            if files:
                yield content, files
                content, files = "", []
            if len(text) > MAX_CONTENT:
                if len(content) and len(content) + len(text) % MAX_CONTENT <= MAX_CONTENT:
                    offset = MAX_CONTENT - len(content)
                else:
                    offset = 0
                content += text[:offset]
                for i in range((len(text) - offset - 1) // MAX_CONTENT + 1):
                    if content or files:
                        yield content, files
                        content, files = "", []
                    content = text[offset + i * MAX_CONTENT : offset + (i + 1) * MAX_CONTENT]
            else:
                if len(content) + len(text) > MAX_CONTENT:
                    yield content, files
                    content, files = "", []
                content += text
        elif isinstance(item, CodeItem): # type: ignore
            if len(item.text) > MAX_CONTENT or len(str(CodeBlock(item.text, language=item.language))) > MAX_CONTENT:
                if len(files) >= MAX_FILES:
                    yield content, files
                    content, files = "", []
                files.append(File(BytesIO(item.text.encode("utf8")), filename=item.filename))
            else:
                text = str(CodeBlock(item.text, language=item.language))
                if files or len(content) + len(text) > MAX_CONTENT:
                    yield content, files
                    content, files = "", []
                content += text
    if content or files:
        yield content, files
