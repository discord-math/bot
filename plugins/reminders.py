from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

import discord
from discord import AllowedMentions, MessageReference, Object, TextChannel, Thread
from discord.ext.commands import command, group
from sqlalchemy import TEXT, TIMESTAMP, BigInteger, delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.functions import current_timestamp

from bot.acl import EvalResult, evaluate_ctx, privileged, register_action
from bot.client import client
from bot.commands import Context, cleanup, plugin_command
from bot.tasks import task
import plugins
import util.db.kv
from util.discord import DurationConverter, PlainItem, UserError, chunk_messages, format


registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)


@registry.mapped
class Reminder:
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    time: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    content: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:

        def __init__(
            self, *, user_id: int, guild_id: int, channel_id: int, message_id: int, time: datetime, content: str
        ) -> None: ...


logger = logging.getLogger(__name__)
manage_reminders = register_action("manage_reminders")  # For use in removing reminders


def format_msg(guild_id: int, channel_id: int, msg_id: int) -> str:
    return "https://discord.com/channels/{}/{}/{}".format(guild_id, channel_id, msg_id)


def format_reminder(reminder: Reminder) -> str:
    msg = format_msg(reminder.guild_id, reminder.channel_id, reminder.message_id)
    if reminder.content == "":
        return format("{} for {!f}", msg, reminder.time)
    return format("{!i} ({}) for {!f}", reminder.content, msg, reminder.time)


async def send_reminder(reminder: Reminder) -> None:
    guild = client.get_guild(reminder.guild_id)
    if guild is None:
        logger.info(
            "Reminder {} for user {} silently removed (guild no longer exists)".format(reminder.id, reminder.user_id)
        )
        return
    try:
        channel = await guild.fetch_channel(reminder.channel_id)
    except discord.NotFound:
        logger.info(
            "Reminder {} for user {} silently removed (channel no longer exists)".format(reminder.id, reminder.user_id)
        )
        return
    if not isinstance(channel, (TextChannel, Thread)):
        logger.info(
            "Reminder {} for user {} silently removed (invalid channel type)".format(reminder.id, reminder.user_id)
        )
        return
    try:
        creation_time = discord.utils.snowflake_time(reminder.message_id)
        await channel.send(
            format(
                "{!m} asked to be reminded {!R}, {}",
                reminder.user_id,
                creation_time,
                reminder.content,
            )[:2000],
            reference=MessageReference(
                message_id=reminder.message_id, channel_id=reminder.channel_id, fail_if_not_exists=False
            ),
            allowed_mentions=AllowedMentions(everyone=False, users=[Object(reminder.user_id)], roles=False),
        )
    except discord.Forbidden:
        logger.info("Reminder {} for user {} silently removed (permission error)".format(reminder.id, reminder.user_id))


@task(name="Reminder expiry task", every=86400, exc_backoff_base=60)
async def expiry_task() -> None:
    await client.wait_until_ready()

    async with sessionmaker() as session:
        stmt = delete(Reminder).where(Reminder.time <= func.timezone("UTC", current_timestamp())).returning(Reminder)
        for reminder in (await session.execute(stmt)).scalars():
            logger.debug("Expiring reminder for user #{}".format(reminder.user_id))
            await send_reminder(reminder)
        await session.commit()

        stmt = select(Reminder.time).order_by(Reminder.time).limit(1)
        next_expiry = (await session.execute(stmt)).scalar()

    if next_expiry is not None:
        delay = next_expiry - datetime.utcnow()
        expiry_task.run_coalesced(delay.total_seconds())
        logger.debug("Waiting for next reminder to expire in {}".format(delay))


@plugins.init
async def init() -> None:
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        for (user_id,) in conf:
            for reminder in cast(List[Dict[str, Any]], conf[user_id]):
                session.add(
                    Reminder(
                        user_id=int(user_id),
                        guild_id=reminder["guild"],
                        channel_id=reminder["channel"],
                        message_id=reminder["msg"],
                        time=datetime.fromtimestamp(reminder["time"]),
                        content=reminder["contents"],
                    )
                )
        await session.commit()
        for user_id in [user_id for user_id, in conf]:
            conf[user_id] = None
        await conf

    expiry_task.run_coalesced(0)


@plugin_command
@cleanup
@command("remindme", aliases=["remind"])
@privileged
async def remindme_command(ctx: Context, interval: DurationConverter, *, text: Optional[str]) -> None:
    """Set a reminder with a given message."""
    if ctx.guild is None:
        raise UserError("Only usable in a server")

    async with sessionmaker() as session:
        reminder = Reminder(
            user_id=ctx.author.id,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
            time=datetime.utcnow() + interval,
            content=text or "",
        )
        session.add(reminder)
        await session.commit()

    expiry_task.run_coalesced(0)

    await ctx.send(
        "Created reminder {}".format(format_reminder(reminder))[:2000], allowed_mentions=AllowedMentions.none()
    )


@plugin_command
@cleanup
@group("reminder", aliases=["reminders"], invoke_without_command=True)
@privileged
async def reminder_command(ctx: Context) -> None:
    """Display your reminders."""
    async with sessionmaker() as session:
        stmt = select(Reminder).where(Reminder.user_id == ctx.author.id)
        reminders = (await session.execute(stmt)).scalars()

    items = [PlainItem("Your reminders include:\n")]
    for reminder in reminders:
        items.append(PlainItem("**{}.** Reminder {}\n".format(reminder.id, format_reminder(reminder))))
    for content, _ in chunk_messages(items):
        await ctx.send(content, allowed_mentions=AllowedMentions.none())


@reminder_command.command("remove")
@privileged
async def reminder_remove(ctx: Context, id: int) -> None:
    """Delete a reminder."""
    async with sessionmaker() as session:
        if reminder := await session.get(Reminder, id):
            # To remove another user's reminders you need elevated permissions
            if reminder.user_id != ctx.author.id:
                if manage_reminders.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                    raise UserError("Reminder {} is owned by a different user.".format(id))
            await session.delete(reminder)
            await session.commit()
            await ctx.send(
                "Removed reminder {}".format(format_reminder(reminder))[:2000], allowed_mentions=AllowedMentions.none()
            )

            expiry_task.run_coalesced(0)
        else:
            raise UserError("Reminder {} does not exist".format(id))
