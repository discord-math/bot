"""
Utilities for registering basic commands. Commands are triggered by a configurable prefix.
"""

import re
import asyncio
import logging
import discord
import discord.ext.commands
import discord.ext.typed_commands
from typing import (Dict, Iterator, Optional, Callable, Awaitable, Coroutine, Any, Type, TypeVar, Protocol, cast,
    overload)
import util.discord
import discord_client
import util.db.kv
import plugins

class CommandsConfig(Protocol):
    prefix: str

conf: CommandsConfig
logger: logging.Logger = logging.getLogger(__name__)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(CommandsConfig, await util.db.kv.load(__name__))

class Arg:
    __slots__ = "source", "rest"
    source: str
    rest: str

    def __init__(self, source: str, rest: str):
        self.source = source
        self.rest = rest

class TagArg(Arg):
    __slots__ = "id"
    id: int

    def __init__(self, source: str, rest: str, id: int):
        super().__init__(source, rest)
        self.id = id

class UserMentionArg(TagArg):
    __slots__ = "has_nick"
    has_nick: bool

    def __init__(self, source: str, rest: str, id: int, has_nick: bool):
        super().__init__(source, rest, id)
        self.has_nick = has_nick

class RoleMentionArg(TagArg):
    __slots__ = ()

class ChannelArg(TagArg):
    __slots__ = ()

class EmojiArg(TagArg):
    __slots__ = "name", "animated"
    name: str
    animated: bool

    def __init__(self, source: str, rest: str, id: int, name: str, animated: bool):
        super().__init__(source, rest, id)
        self.name = name
        self.animated = animated

class StringArg(Arg):
    __slots__ = "text"
    text: str

    def __init__(self, source: str, rest: str, text: str):
        super().__init__(source, rest)
        self.text = text

class InlineCodeArg(StringArg):
    __slots__ = ()

class CodeBlockArg(StringArg):
    __slots__ = "language"
    language: Optional[str]

    def __init__(self, source: str, rest: str, text: str,
        language: Optional[str]):
        super().__init__(source, rest, text)
        self.language = language

class ArgParser:
    """
    Parse a commandline into a sequence of words, quoted strings, code blocks, user or role mentions, channel links, and
    optionally emojis.
    """

    __slots__ = "cmdline", "pos"
    cmdline: str
    pos: int

    def __init__(self, text: str):
        self.cmdline = text.lstrip()
        self.pos = 0

    parse_re = r"""
        \s*
        (?P<source> # tag:
            (?P<tag><
                (?P<type>@!?|@&|[#]{})
                (?P<id>\d+)
            >)
        |   # quoted string:
            "
                (?P<string>(?:\\.|[^"])*)
            "
        |   # code block:
            ```
                (?P<language>\S*\n(?!```))?
                (?P<block>(?:(?!```).)+)
            ```
        |   # inline code:
            ``
                (?P<code2>(?:(?!``).)+)
            ``
        |   # inline code:
            `
                (?P<code1>[^`]+)
            `
        |   # regular word, up to first tag:
            (?P<text>(?:(?!<(?:@!?|@&|[#]{})\d+>)\S)+)
        )
        """
    parse_re_emoji = re.compile(parse_re.format(r"|(?P<animated>a)?:(?P<emoji>\w*):", r"|a?:\w*:"), re.S | re.X)
    parse_re_no_emoji = re.compile(parse_re.format(r"", r""), re.S | re.X)

    def __iter__(self) -> Iterator[Arg]:
        while (arg := self.next_arg()) is not None:
            yield arg

    def get_rest(self) -> str:
        return self.cmdline[self.pos:].lstrip()

    def next_arg(self, chunk_emoji: bool = False) -> Optional[Arg]:
        regex = self.parse_re_emoji if chunk_emoji else self.parse_re_no_emoji
        match = regex.match(self.cmdline, self.pos)
        if match is None: return None
        self.pos = match.end()
        if chunk_emoji and match["emoji"] != None:
            return EmojiArg(
                match["source"], self.get_rest(), int(match["id"]), match["emoji"], match["animated"] != None)
        elif match["type"] != None:
            type = match["type"]
            id = int(match["id"])
            if type == "@":
                return UserMentionArg(match["source"], self.get_rest(), id, False)
            elif type == "@!":
                return UserMentionArg(match["source"], self.get_rest(), id, True)
            elif type == "@&":
                return RoleMentionArg(match["source"], self.get_rest(), id)
            elif type == "#":
                return ChannelArg(match["source"], self.get_rest(), id)
        elif match["string"] != None:
            return StringArg(match["source"], self.get_rest(), re.sub(r"\\(.)", r"\1", match["string"]))
        elif match["block"] != None:
            return CodeBlockArg(match["source"], self.get_rest(), match["block"], match["language"])
        elif match["code1"] != None:
            return InlineCodeArg(match["source"], self.get_rest(), match["code1"])
        elif match["code2"] != None:
            return InlineCodeArg(match["source"], self.get_rest(), match["code2"])
        elif match["text"] != None:
            return StringArg(match["source"], self.get_rest(), match["text"])
        return None

commands: Dict[str, Callable[[discord.Message, ArgParser], Awaitable[None]]]
commands = {}

def bot_prefix(bot: discord.Client, msg: discord.Message) -> str:
    return conf.prefix
discord_client.client.command_prefix = bot_prefix
@plugins.finalizer
def cleanup_prefix() -> None:
    discord_client.client.command_prefix = ()

@util.discord.event("command")
async def on_command(ctx: discord.ext.commands.Context) -> None:
    logger.info(util.discord.format("Command {!r} from {!m} in {!c}",
        ctx.command.qualified_name, ctx.author.id, ctx.channel.id))
    return

@util.discord.event("command_error")
async def on_command_error(ctx: discord.ext.commands.Context, exc: Exception) -> None:
    if isinstance(exc, discord.ext.commands.CommandNotFound):
        return
    elif isinstance(exc, discord.ext.commands.UserInputError):
        # todo: display usage
        await ctx.send("Error: {}".format(exc.args[0]), allowed_mentions=discord.AllowedMentions.none())
        return
    elif isinstance(exc, discord.ext.commands.CommandInvokeError):
        logger.error(util.discord.format("Error in command {} {!r} {!r} from {!m} in {!c}",
            ctx.command.qualified_name, tuple(ctx.args), ctx.kwargs,
            ctx.author.id, ctx.channel.id), exc_info=exc.__cause__)
        return
    elif isinstance(exc, discord.ext.commands.CommandError):
        await ctx.send("Error: {}".format(exc.args[0]), allowed_mentions=discord.AllowedMentions.none())
        return
    else:
        logger.error(util.discord.format("Unknown exception in command {} {!r} {!r} from {!m} in {!c}",
            ctx.command.qualified_name, tuple(ctx.args), ctx.kwargs), exc_info=exc)
        return

@util.discord.event("message")
async def message_find_command(msg: discord.Message) -> None:
    if conf.prefix and msg.content.startswith(conf.prefix):
        cmdline = msg.content[len(conf.prefix):]
        parser = ArgParser(cmdline)
        cmd = parser.next_arg()
        if isinstance(cmd, StringArg):
            name = cmd.text.lower()
            if name in commands:
                try:
                    logger.info(util.discord.format("Command {!r} from {!m} in {!c}",
                        cmdline, msg.author.id, msg.channel.id))
                    await commands[name](msg, parser)
                except util.discord.UserError as exc:
                    await msg.channel.send("Error ({}): {}".format(type(exc), exc.args[0]),
                        allowed_mentions=discord.AllowedMentions.none())
                except:
                    logger.error(util.discord.format("Error in command {!r} from {!m} in {!c}",
                        name, msg.author.id, msg.channel.id), exc_info=True)
    await discord_client.client.process_commands(msg)

def unsafe_hook_command(name: str, fun: Callable[[discord.Message, ArgParser], Awaitable[None]]) -> None:
    if not asyncio.iscoroutinefunction(fun):
        raise TypeError("expected coroutine function")
    if name in commands:
        raise ValueError("command {} already registered".format(name))
    commands[name] = fun

def unsafe_unhook_command(name: str, fun: Callable[[discord.Message, ArgParser], Awaitable[None]]) -> None:
    if name not in commands:
        raise ValueError("command {} is not registered".format(name))
    del commands[name]

def command(name: str) -> Callable[[Callable[[discord.Message, ArgParser], Awaitable[None]]],
    Callable[[discord.Message, ArgParser], Awaitable[None]]]:
    """
    This decorator registers a function as a command with a given name. The function receives the Message and an
    ArgParser arguments. Only one function can be assigned to a given command name. This registers a finalizer that
    removes the command, so should only be called during plugin initialization.
    """
    def decorator(fun: Callable[[discord.Message, ArgParser], Awaitable[None]]) -> Callable[
        [discord.Message, ArgParser], Awaitable[None]]:
        fun.__name__ = name
        unsafe_hook_command(name, fun)
        @plugins.finalizer
        def cleanup_command() -> None:
            unsafe_unhook_command(name, fun)
        return fun
    return decorator

T = TypeVar("T")

@overload
def command_ext(name: Optional[str] = None, cls: Type[T] = ..., *args: Any, **kwargs: Any) -> Callable[
    [Callable[..., Coroutine[Any, Any, None]]], T]: ...
@overload
def command_ext(name: Optional[str] = None, cls: None = None, *args: Any, **kwargs: Any) -> Callable[
    [Callable[..., Coroutine[Any, Any, None]]], discord.ext.typed_commands.Command[discord.ext.commands.Context]]: ...
def command_ext(name: Optional[str] = None, cls: Any = discord.ext.commands.Command, *args: Any, **kwargs: Any
    ) -> Callable[[Callable[..., Coroutine[Any, Any, None]]], Any]:
    def decorator(fun: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., Coroutine[Any, Any, None]]:
        cmd: discord.ext.typed_commands.Command[discord.ext.commands.Context]
        if isinstance(fun, discord.ext.commands.Command):
            if args or kwargs:
                raise TypeError("the provided object is already a Command (args/kwargs have no effect)")
            cmd = fun
        else:
            cmd = discord.ext.commands.command(name=name, cls=cls, *args, **kwargs)(fun) # type: ignore
        discord_client.client.add_command(cmd)
        @plugins.finalizer
        def cleanup_command() -> None:
            discord_client.client.remove_command(cmd.name)
        return cmd
    return decorator
