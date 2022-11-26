"""
Utilities for registering basic commands. Commands are triggered by a configurable prefix.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional, Protocol, Set, Type, TypeVar, cast, overload
from typing_extensions import Concatenate, ParamSpec

import discord
import discord.ext.commands

import bot.client
import bot.cogs
import plugins
import util.db.kv
import util.discord


class CommandsConfig(Protocol):
    prefix: str

conf: CommandsConfig
logger: logging.Logger = logging.getLogger(__name__)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(CommandsConfig, await util.db.kv.load(__name__))

def bot_prefix(bot: discord.Client, msg: discord.Message) -> str:
    return conf.prefix
bot.client.client.command_prefix = bot_prefix
@plugins.finalizer
def cleanup_prefix() -> None:
    bot.client.client.command_prefix = ()

Context = discord.ext.commands.Context[discord.ext.commands.Bot]

@bot.cogs.cog
class Commands(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_command(self, ctx: Context) -> None:
        logger.info(util.discord.format("Command {!r} from {!m} in {!c}",
            ctx.command and ctx.command.qualified_name, ctx.author.id, ctx.channel.id))

    @discord.ext.commands.Cog.listener()
    async def on_command_error(self, ctx: Context, exc: Exception) -> None:
        try:
            if isinstance(exc, discord.ext.commands.CommandNotFound):
                return
            elif isinstance(exc, discord.ext.commands.CheckFailure):
                return
            elif isinstance(exc, discord.ext.commands.UserInputError):
                if isinstance(exc, discord.ext.commands.BadUnionArgument):
                    def conv_name(conv: Any) -> Any:
                        try:
                            return conv.__name__
                        except AttributeError:
                            if hasattr(conv, '__origin__'):
                                return repr(conv)
                            return conv.__class__.__name__

                    exc_str = "Could not interpret \"{}\" as:\n{}".format(exc.param.name,
                        "\n".join("{}: {}".format(conv_name(conv), sub_exc)
                            for conv, sub_exc in zip(exc.converters, exc.errors)))
                else:
                    exc_str = str(exc)
                message = "Error: {}".format(exc_str)
                if ctx.command is not None:
                    if getattr(ctx.command, "suppress_usage", False):
                        return
                    if ctx.invoked_with is not None and ctx.invoked_parents is not None:
                        usage = " ".join(
                            s for s in ctx.invoked_parents + [ctx.invoked_with, ctx.command.signature] if s)
                    else:
                        usage = " ".join(s for s in [ctx.command.qualified_name, ctx.command.signature] if s)
                    message += util.discord.format("\nUsage: {!i}", usage)
                await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
                return
            elif isinstance(exc, discord.ext.commands.CommandInvokeError):
                logger.error(util.discord.format("Error in command {} {!r} {!r} from {!m} in {!c}",
                    ctx.command and ctx.command.qualified_name, tuple(ctx.args), ctx.kwargs,
                    ctx.author.id, ctx.channel.id), exc_info=exc.__cause__)
                return
            elif isinstance(exc, discord.ext.commands.CommandError):
                await ctx.send("Error: {}".format(str(exc)), allowed_mentions=discord.AllowedMentions.none())
                return
            else:
                logger.error(util.discord.format("Unknown exception in command {} {!r} {!r} from {!m} in {!c}",
                    ctx.command and ctx.command.qualified_name, tuple(ctx.args), ctx.kwargs), exc_info=exc)
                return
        finally:
            await finalize_cleanup(ctx)

    @discord.ext.commands.Cog.listener()
    async def on_message(self, msg: discord.Message) -> None:
        await bot.client.client.process_commands(msg)

T = TypeVar("T")
P = ParamSpec("P")
BotT = TypeVar('BotT', bound=discord.ext.commands.Bot, covariant=True)
ContextT = TypeVar('ContextT', bound=Context)
CogT = TypeVar("CogT", bound=Optional[discord.ext.commands.Cog])
FreeCommandT = TypeVar("FreeCommandT", bound=discord.ext.commands.Command[None, Any, Any])
CommandT = TypeVar("CommandT", bound=discord.ext.commands.Command[Any, Any, Any])

@overload
def command(name: Optional[str] = None, cls: Type[FreeCommandT] = ..., *args: Any, **kwargs: Any) -> Callable[
    [Callable[Concatenate[ContextT, P], Coroutine[Any, Any, Any]]], FreeCommandT]: ...
@overload
def command(name: Optional[str] = None, cls: None = None, *args: Any, **kwargs: Any) -> Callable[
    [Callable[Concatenate[ContextT, P], Coroutine[Any, Any, T]]],
    discord.ext.commands.Command[None, P, T]]: ...
def command(name: Optional[str] = None, cls: Any = discord.ext.commands.Command, *args: Any, **kwargs: Any) -> Callable[
    [Callable[Concatenate[discord.ext.commands.Context[Any], P], Coroutine[Any, Any, Any]]], Any]:
    def decorator(fun: Callable[Concatenate[ContextT, P], Coroutine[Any, Any, T]]
        ) -> Callable[Concatenate[ContextT, P], Coroutine[Any, Any, T]]:
        cmd: discord.ext.commands.Command[None, P, T]
        if isinstance(fun, discord.ext.commands.Command):
            if args or kwargs:
                raise TypeError("the provided object is already a Command (args/kwargs have no effect)")
            cmd = fun
        else:
            cmd = discord.ext.commands.command(name=name, cls=cls, *args, **kwargs)(fun) # type: ignore
        bot.client.client.add_command(cmd)
        def cleanup_command() -> None:
            bot.client.client.remove_command(cmd.name)
        plugins.finalizer(cleanup_command)
        return cmd
    return decorator

def group(name: Optional[str] = None, *args: Any, **kwargs: Any) -> Callable[
    [Callable[Concatenate[ContextT, P], Coroutine[Any, Any, T]]], discord.ext.commands.Group[None, P, T]]:
    return command(name, cls=discord.ext.commands.Group, *args, **kwargs)

def suppress_usage(cmd: T) -> T:
    cmd.suppress_usage = True # type: ignore
    return cmd

class CleanupContext(discord.ext.commands.Context[BotT]):
    cleanup: "CleanupReference"

class CleanupReference:
    __slots__ = "messages", "task"
    messages: Set[discord.PartialMessage]
    task: Optional[asyncio.Task[None]]

    def __init__(self, ctx: CleanupContext[BotT]):
        self.messages = set()
        chan_id = ctx.channel.id
        msg_id = ctx.message.id
        async def cleanup_task() -> None:
            await ctx.bot.wait_for("raw_message_delete",
                check=lambda m: m.channel_id == chan_id and m.message_id == msg_id)
        self.task = asyncio.create_task(cleanup_task(), name="Cleanup task for {}-{}".format(chan_id, msg_id))

    def __del__(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def add(self, msg: discord.Message) -> None:
        self.messages.add(discord.PartialMessage(channel=msg.channel, id=msg.id))

    async def finalize(self) -> None:
        if self.task is None:
            return
        try:
            if len(self.messages) != 0:
                await asyncio.wait_for(self.task, 300)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        else:
            for msg in self.messages:
                try:
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        finally:
            self.task.cancel()
            self.task = None

def init_cleanup(ctx: CleanupContext[BotT]) -> None:
    if not hasattr(ctx, "cleanup"):
        ref = CleanupReference(ctx)
        ctx.cleanup = ref

        old_send = ctx.send
        async def send(*args: Any, **kwargs: Any) -> discord.Message:
            msg = await old_send(*args, **kwargs)
            ref.add(msg)
            return msg
        ctx.send = send

async def finalize_cleanup(ctx: ContextT) -> None:
    if (ref := getattr(ctx, "cleanup", None)) is not None:
        await ref.finalize()

"""Mark a message as "output" of a cleanup command."""
def add_cleanup(ctx: ContextT, msg: discord.Message) -> None:
    if (ref := getattr(ctx, "cleanup", None)) is not None:
        ref.add(msg)

"""Make the command watch out for the deletion of the invoking message, and in that case, delete all output."""
def cleanup(cmd: CommandT) -> CommandT:
    old_invoke = cmd.invoke
    async def invoke(ctx: CleanupContext[BotT]) -> None:
        init_cleanup(ctx)
        await old_invoke(ctx)
        await finalize_cleanup(ctx)
    cmd.invoke = invoke # type: ignore

    old_on_error = getattr(cmd, "on_error", None)
    async def on_error(*args: Any) -> None:
        if len(args) == 3:
            _, ctx, _ = args
        else:
            ctx, _ = args
        init_cleanup(ctx)
        if old_on_error is not None:
            await old_on_error(*args)
    cmd.on_error = on_error

    old_ensure_assignment_on_copy = cmd._ensure_assignment_on_copy
    def ensure_assignment_on_copy(other: CommandT) -> CommandT:
        return cleanup(old_ensure_assignment_on_copy(other))
    cmd._ensure_assignment_on_copy = ensure_assignment_on_copy

    return cmd