"""
Utilities for registering basic commands. Commands are triggered by a configurable prefix.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional, Set, TypeVar

import discord
from discord import AllowedMentions, Message, PartialMessage
import discord.ext.commands
from discord.ext.commands import (
    BadUnionArgument,
    Bot,
    CheckFailure,
    Cog,
    Command,
    CommandError,
    CommandInvokeError,
    CommandNotFound,
    NoPrivateMessage,
    PrivateMessageOnly,
    UserInputError,
)
from sqlalchemy import TEXT, BigInteger, Computed
from sqlalchemy.ext.asyncio import AsyncSession
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column

from bot.client import client
from bot.cogs import cog
import plugins
import util.db.kv
from util.discord import format


registry = sqlalchemy.orm.registry()


@registry.mapped
class GlobalConfig:
    __tablename__ = "commands_config"
    id: Mapped[int] = mapped_column(BigInteger, Computed("0"), primary_key=True)
    prefix: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:

        def __init__(self, *, prefix: str, id: int = ...) -> None:
            ...


logger: logging.Logger = logging.getLogger(__name__)
prefix: str


@plugins.init
async def init() -> None:
    global prefix
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))

    async with AsyncSession(util.db.engine, expire_on_commit=False) as session:
        conf = await session.get(GlobalConfig, 0)
        if not conf:
            conf = GlobalConfig(prefix=str((await util.db.kv.load(__name__)).prefix))
            session.add(conf)
            await session.commit()

        prefix = client.command_prefix = conf.prefix


@plugins.finalizer
def cleanup_prefix() -> None:
    client.command_prefix = ()


Context = discord.ext.commands.Context[Bot]


@cog
class Commands(Cog):
    @Cog.listener()
    async def on_command(self, ctx: Context) -> None:
        logger.info(
            format(
                "Command {!r} from {!m} in {!c}",
                ctx.command and ctx.command.qualified_name,
                ctx.author.id,
                ctx.channel.id,
            )
        )

    @Cog.listener()
    async def on_command_error(self, ctx: Context, exc: Exception) -> None:
        try:
            if isinstance(exc, CommandNotFound):
                return
            elif isinstance(exc, CheckFailure) and not isinstance(exc, (NoPrivateMessage, PrivateMessageOnly)):
                return
            elif isinstance(exc, UserInputError):
                if isinstance(exc, BadUnionArgument):

                    def conv_name(conv: type) -> str:
                        try:
                            return conv.__name__
                        except AttributeError:
                            if hasattr(conv, "__origin__"):
                                return repr(conv)
                            return conv.__class__.__name__

                    exc_str = 'Could not interpret "{}" as:\n{}'.format(
                        exc.param.name,
                        "\n".join(
                            "- {}: {}".format(conv_name(conv), sub_exc)
                            for conv, sub_exc in zip(exc.converters, exc.errors)
                        ),
                    )
                else:
                    exc_str = str(exc)
                message = "Error: {}".format(exc_str)
                if ctx.command is not None:
                    if getattr(ctx.command, "suppress_usage", False):
                        return
                    if ctx.invoked_with is not None and ctx.invoked_parents is not None:
                        usage = " ".join(
                            s for s in ctx.invoked_parents + [ctx.invoked_with, ctx.command.signature] if s
                        )
                    else:
                        usage = " ".join(s for s in [ctx.command.qualified_name, ctx.command.signature] if s)
                    message += format("\nUsage: {!i}", usage)
                await ctx.send(message, allowed_mentions=AllowedMentions.none())
                return
            elif isinstance(exc, CommandInvokeError):
                logger.error(
                    format(
                        "Error in command {} {!r} {!r} from {!m} in {!c}",
                        ctx.command and ctx.command.qualified_name,
                        tuple(ctx.args),
                        ctx.kwargs,
                        ctx.author.id,
                        ctx.channel.id,
                    ),
                    exc_info=exc.__cause__,
                )
                return
            elif isinstance(exc, CommandError):
                await ctx.send("Error: {}".format(str(exc)), allowed_mentions=AllowedMentions.none())
                return
            else:
                logger.error(
                    format(
                        "Unknown exception in command {} {!r} {!r} from {!m} in {!c}",
                        ctx.command and ctx.command.qualified_name,
                        tuple(ctx.args),
                        ctx.kwargs,
                    ),
                    exc_info=exc,
                )
                return
        finally:
            await finalize_cleanup(ctx)

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        await client.process_commands(msg)


CommandT = TypeVar("CommandT", bound=Command[Any, Any, Any])


def plugin_command(cmd: CommandT) -> CommandT:
    """
    Register a command to be added/removed together with the plugin. The command must be already wrapped in
    discord.ext.commands.command or discord.ext.commands.group.
    """
    client.add_command(cmd)
    plugins.finalizer(lambda: client.remove_command(cmd.name))
    return cmd


def suppress_usage(cmd: CommandT) -> CommandT:
    """This decorator on a command suppresses the usage instructions if the command is invoked incorrectly."""
    cmd.suppress_usage = True  # type: ignore
    return cmd


BotT = TypeVar("BotT", bound=Bot, covariant=True)


class CleanupContext(discord.ext.commands.Context[BotT]):
    cleanup: "CleanupReference"


class CleanupReference:
    __slots__ = "messages", "task"
    messages: Set[PartialMessage]
    task: Optional[asyncio.Task[None]]

    def __init__(self, ctx: CleanupContext[BotT]):
        self.messages = set()
        chan_id = ctx.channel.id
        msg_id = ctx.message.id

        async def cleanup_task() -> None:
            await ctx.bot.wait_for(
                "raw_message_delete", check=lambda m: m.channel_id == chan_id and m.message_id == msg_id
            )

        self.task = asyncio.create_task(cleanup_task(), name="Cleanup task for {}-{}".format(chan_id, msg_id))

    def __del__(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def add(self, msg: Message) -> None:
        self.messages.add(PartialMessage(channel=msg.channel, id=msg.id))

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

        async def send(*args: Any, **kwargs: Any) -> Message:
            msg = await old_send(*args, **kwargs)
            ref.add(msg)
            return msg

        ctx.send = send


async def finalize_cleanup(ctx: object) -> None:
    if (ref := getattr(ctx, "cleanup", None)) is not None:
        await ref.finalize()


def add_cleanup(ctx: object, msg: Message) -> None:
    """Mark a message as "output" of a cleanup command."""
    if (ref := getattr(ctx, "cleanup", None)) is not None:
        ref.add(msg)


def cleanup(cmd: CommandT) -> CommandT:
    """Make the command watch out for the deletion of the invoking message, and in that case, delete all output."""
    old_invoke = cmd.invoke

    async def invoke(ctx: CleanupContext[BotT]) -> None:
        init_cleanup(ctx)
        await old_invoke(ctx)
        await finalize_cleanup(ctx)

    cmd.invoke = invoke  # type: ignore

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
