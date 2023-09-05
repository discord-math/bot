import asyncio
from datetime import datetime, timezone
import logging
from operator import itemgetter
import time
from typing import Awaitable, Iterator, Optional, Protocol, Tuple, TypedDict, cast

import discord
from discord import AllowedMentions, MessageReference, Object, TextChannel, Thread

from bot.client import client
from bot.commands import Context, cleanup, command, group
from bot.privileges import priv
import plugins
import util.db.kv
from util.discord import DurationConverter, PlainItem, UserError, chunk_messages, format
from util.frozen_list import FrozenList

class Reminder(TypedDict):
    guild: int
    channel: int
    msg: int
    time: int
    contents: str

class RemindersConf(Awaitable[None], Protocol):
    def __getitem__(self, user_id: int) -> Optional[FrozenList[Reminder]]: ...
    def __setitem__(self, user_id: int, obj: Optional[FrozenList[Reminder]]) -> None: ...
    def __iter__(self) -> Iterator[Tuple[str]]: ...

conf: RemindersConf
logger = logging.getLogger(__name__)

def format_msg(guild_id: int, channel_id: int, msg_id: int) -> str:
    return "https://discord.com/channels/{}/{}/{}".format(guild_id, channel_id, msg_id)

def format_reminder(reminder: Reminder) -> str:
    guild, channel, msg, send_time, contents = itemgetter("guild", "channel", "msg", "time", "contents")(reminder)
    if contents == "": return "{} for <t:{}:F>".format(format_msg(guild, channel, msg), send_time)
    return format("{!i} ({}) for <t:{}:F>", contents, format_msg(guild, channel, msg), send_time)

def format_text_reminder(reminder: Reminder) -> str:
    guild, channel, msg, send_time, contents = itemgetter("guild", "channel", "msg", "time", "contents")(reminder)
    time_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(send_time))
    if contents == "": return '{} for {}'.format(format_msg(guild, channel, msg), time_str)
    return '"""{}""" ({}) for {}'.format(contents, format_msg(guild, channel, msg), time_str)

async def send_reminder(user_id: int, reminder: Reminder) -> None:
    guild = client.get_guild(reminder["guild"])
    if guild is None:
        logger.info("Reminder {} for user {} silently removed (guild no longer exists)".format(str(reminder), user_id))
        return
    channel = guild.get_channel_or_thread(reminder["channel"])
    if not isinstance(channel, (TextChannel, Thread)):
        logger.info("Reminder {} for user {} silently removed (channel no longer exists)".format(str(reminder), user_id))
        return
    try:
        creation_time = discord.utils.snowflake_time(reminder["msg"]).replace(tzinfo=timezone.utc)
        await channel.send(format("{!m} asked to be reminded <t:{}:R>: {}",
                user_id, int(creation_time.timestamp()), reminder["contents"])[:2000],
            reference = MessageReference(message_id=reminder["msg"],
                channel_id=reminder["channel"], fail_if_not_exists=False),
            allowed_mentions=AllowedMentions(everyone=False, users=[Object(user_id)], roles=False))
    except discord.Forbidden:
        logger.info("Reminder {} for user {} silently removed (permission error)".format(str(reminder), user_id))

async def handle_reminder(user_id: int, reminder: Reminder) -> None:
    await send_reminder(user_id, reminder)

    reminders_optional = conf[user_id]
    if reminders_optional is None: return
    conf[user_id] = reminders_optional.without(reminder)
    await conf

expiration_updated = asyncio.Semaphore(value=0)

async def expire_reminders() -> None:
    await client.wait_until_ready()

    while True:
        try:
            now = datetime.now(timezone.utc).timestamp()
            next_expiry = None
            for user_id, in conf:
                reminders = conf[int(user_id)]
                if reminders is None: continue
                for reminder in reminders:
                    if int(reminder["time"]) < now:
                        logger.debug("Expiring reminder for user #{}".format(user_id))
                        await handle_reminder(int(user_id), reminder)
                    elif next_expiry is None or int(reminder["time"]) < next_expiry:
                        next_expiry = int(reminder["time"])
            delay = 86400.0
            if next_expiry is not None:
                delay = next_expiry - now
                logger.debug("Waiting for next reminder to expire in {} seconds".format(delay))
            try:
                await asyncio.wait_for(expiration_updated.acquire(), timeout=delay)
                while True:
                    await asyncio.wait_for(expiration_updated.acquire(), timeout=1)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in reminder expiry task", exc_info=True)
            await asyncio.sleep(60)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RemindersConf, await util.db.kv.load(__name__))

    for user_id, in conf:
        obj = conf[int(user_id)]
        assert obj is not None
        conf[int(user_id)] = FrozenList(Reminder(guild=int(rem["guild"]), channel=int(rem["channel"]),
            msg=int(rem["msg"]), time=int(rem["time"]), contents=rem["contents"]) for rem in obj)
    await conf

    expiry_task: asyncio.Task[None] = asyncio.create_task(expire_reminders(), name="Reminders")
    plugins.finalizer(expiry_task.cancel)

@cleanup
@command("remindme", aliases=["remind"])
@priv("remind")
async def remindme_command(ctx: Context, interval: DurationConverter, *, text: Optional[str]) -> None:
    """Set a reminder with a given message."""
    if ctx.guild is None:
        raise UserError("Only usable in a guild")

    reminders_optional = conf[ctx.author.id]
    reminders = reminders_optional.copy() if reminders_optional is not None else []
    reminder_time = int((datetime.now(timezone.utc) + interval).timestamp())
    reminder = Reminder(guild=ctx.guild.id, channel=ctx.channel.id, msg=ctx.message.id, time=reminder_time,
        contents=text or "")
    reminders.append(reminder)
    reminders.sort(key=lambda a: a["time"])
    conf[ctx.author.id] = FrozenList(reminders)
    await conf
    expiration_updated.release()

    await ctx.send("Created reminder {}".format(format_reminder(reminder))[:2000],
        allowed_mentions=AllowedMentions.none())

@cleanup
@group("reminder", aliases=["reminders"], invoke_without_command=True)
@priv("remind")
async def reminder_command(ctx: Context) -> None:
    """Display your reminders."""
    reminders = conf[ctx.author.id] or FrozenList()
    items = [PlainItem("Your reminders include:\n")]
    for i, reminder in enumerate(reminders, start=1):
        items.append(PlainItem("**{}.** Reminder {}\n".format(i, format_reminder(reminder))))
    for content, _ in chunk_messages(items):
        await ctx.send(content, allowed_mentions=AllowedMentions.none())

@reminder_command.command("remove")
@priv("remind")
async def reminder_remove(ctx: Context, index: int) -> None:
    """Delete a reminder."""
    reminders_optional = conf[ctx.author.id]
    reminders = reminders_optional.copy() if reminders_optional is not None else []
    if index < 1 or index > len(reminders):
        raise UserError("Reminder {:d} does not exist".format(index))
    reminder = reminders[index - 1]
    del reminders[index - 1]
    conf[ctx.author.id] = FrozenList(reminders)
    await conf
    expiration_updated.release()
    await ctx.send("Removed reminder {}".format(format_reminder(reminder))[:2000],
        allowed_mentions=AllowedMentions.none())
