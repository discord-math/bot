"""
Utilities for registering basic commands. Commands are triggered by a configurable prefix.
"""

import re
import asyncio
import logging
import discord
import discord.ext.commands
from typing import (Dict, Set, Iterator, Optional, Callable, Awaitable, Coroutine, Any, Type, TypeVar, Protocol, cast,
    overload)
import util.discord
import discord_client
import util.db.kv
import plugins
import plugins.cogs

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
discord_client.client.command_prefix = bot_prefix
@plugins.finalizer
def cleanup_prefix() -> None:
    discord_client.client.command_prefix = ()

@plugins.cogs.cog
class Commands(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_command(self, ctx: discord.ext.commands.Context) -> None:
        logger.info(util.discord.format("Command {!r} from {!m} in {!c}",
            ctx.command and ctx.command.qualified_name, ctx.author.id, ctx.channel.id))

    @discord.ext.commands.Cog.listener()
    async def on_command_error(self, ctx: discord.ext.commands.Context, exc: Exception) -> None:
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
        await discord_client.client.process_commands(msg)

T = TypeVar("T", bound=discord.ext.commands.Command)

@overload
def command(name: Optional[str] = None, cls: Type[T] = ..., *args: Any, **kwargs: Any) -> Callable[
    [Callable[..., Coroutine[Any, Any, None]]], T]: ...
@overload
def command(name: Optional[str] = None, cls: None = None, *args: Any, **kwargs: Any) -> Callable[
    [Callable[..., Coroutine[Any, Any, None]]], discord.ext.commands.Command]: ...
def command(name: Optional[str] = None, cls: Any = discord.ext.commands.Command, *args: Any, **kwargs: Any) -> Callable[
    [Callable[..., Coroutine[Any, Any, None]]], Any]:
    def decorator(fun: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., Coroutine[Any, Any, None]]:
        cmd: discord.ext.commands.Command
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

def group(name: Optional[str] = None, *args: Any, **kwargs: Any) -> Callable[
    [Callable[..., Coroutine[Any, Any, None]]], discord.ext.commands.Group]:
    return command(name, cls=discord.ext.commands.Group, *args, **kwargs) # type: ignore

def suppress_usage(cmd: T) -> T:
    cmd.suppress_usage = True # type: ignore
    return cmd

class CleanupReference:
    __slots__ = "messages", "task"
    messages: Set[discord.PartialMessage]
    task: Optional[asyncio.Task[None]]

    def __init__(self, ctx: discord.ext.commands.Context):
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
        self.messages.add(discord.PartialMessage(channel=msg.channel, id=msg.id)) # type: ignore

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

def init_cleanup(ctx: discord.ext.commands.Context) -> None:
    if not hasattr(ctx, "cleanup"):
        ref = CleanupReference(ctx)
        ctx.cleanup = ref # type: ignore

        old_send = ctx.send
        async def send(*args: Any, **kwargs: Any) -> discord.Message:
            msg = await old_send(*args, **kwargs)
            ref.add(msg)
            return msg
        ctx.send = send # type: ignore

async def finalize_cleanup(ctx: discord.ext.commands.Context) -> None:
    if (ref := getattr(ctx, "cleanup", None)) is not None:
        await ref.finalize()

"""Mark a message as "output" of a cleanup command."""
def add_cleanup(ctx: discord.ext.commands.Context, msg: discord.Message) -> None:
    if (ref := getattr(ctx, "cleanup", None)) is not None:
        ref.add(msg)

"""Make the command watch out for the deletion of the invoking message, and in that case, delete all output."""
def cleanup(cmd: T) -> T:
    old_invoke = cmd.invoke
    async def invoke(ctx: discord.ext.commands.Context) -> None:
        init_cleanup(ctx)
        await old_invoke(ctx)
        await finalize_cleanup(ctx)
    cmd.invoke = invoke # type: ignore

    old_on_error = getattr(cmd, "on_error", None)
    async def on_error(*args: Any) -> None:
        if len(args) == 3:
            cog, ctx, exc = args
        else:
            ctx, exc = args
        init_cleanup(ctx)
        if old_on_error is not None:
            await old_on_error(*args)
    cmd.on_error = on_error

    old_ensure_assignment_on_copy = cmd._ensure_assignment_on_copy # type: ignore
    def ensure_assignment_on_copy(other: T) -> T:
        return cleanup(old_ensure_assignment_on_copy(other)) # type: ignore
    cmd._ensure_assignment_on_copy = ensure_assignment_on_copy # type: ignore

    return cmd
