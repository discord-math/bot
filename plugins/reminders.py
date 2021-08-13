import asyncio
from datetime import timezone, datetime
import discord
from operator import itemgetter
import io
from itertools import count
import re
import time
from typing import Iterator, Optional, Protocol, TypedDict, cast
import discord_client
import logging
import plugins.commands
import plugins.privileges
import util.db.kv
from util.frozen_list import FrozenList

class Reminder(TypedDict):
    guild: str
    channel: str
    msg: str
    time: str
    contents: str

class RemindersConf(Protocol):
    def __getitem__(self, user_id: str) -> Optional[FrozenList[Reminder]]: ...
    def __setitem__(self, user_id: str, obj: Optional[FrozenList[Reminder]]) -> None: ...
    def __iter__(self) -> Iterator[str]: ...

conf: RemindersConf
logger = logging.getLogger(__name__)

time_re = re.compile(
    r"""
    \s*(-?\d+)\s*(?:
    (?P<seconds> s(?:ec(?:ond)?s?)?) |
    (?P<minutes> min(?:ute)?s? | (?!mo)(?-i:m)) |
    (?P<hours> h(?:(?:ou)?rs?)?) |
    (?P<days> d(?:ays?)?) |
    (?P<weeks> w(?:(?:ee)?ks?)) |
    (?P<months> months? | (?-i:M)) |
    (?P<years> y(?:(?:ea)?rs?)?))
    """,
    re.VERBOSE | re.IGNORECASE
)

time_expansion = {
    "seconds": 1,
    "minutes": 60,
    "hours": 60 * 60,
    "days": 60 * 60 * 24,
    "weeks": 60 * 60 * 24 * 7,
    "months": 60 * 60 * 24 * 30,
    "years": 60 * 60 * 24 * 365
}

def get_time(args: plugins.commands.ArgParser) -> Optional[int]:
    time_arg = args.next_arg()
    if not isinstance(time_arg, plugins.commands.StringArg): return None
    time_str = time_arg.text
    seconds = 0
    pos = 0
    while (time_match := time_re.match(time_str, pos)) is not None:
        pos = time_match.end()
        assert time_match.lastgroup is not None
        seconds += int(time_match[1]) * time_expansion[time_match.lastgroup]
    if not (pos == len(time_str) and seconds > 0): return None
    return seconds

def format_msg(guild_id: str, channel_id: str, msg_id: str) -> str:
    return "https://discord.com/channels/{}/{}/{}".format(guild_id, channel_id, msg_id)

def format_reminder(reminder: Reminder) -> str:
    guild, channel, msg, send_time, contents = itemgetter("guild", "channel", "msg", "time", "contents")(reminder)
    if contents == "": return "{} for <t:{}:F>".format(format_msg(guild, channel, msg), send_time)
    return util.discord.format("{!i} ({}) for <t:{}:F>", contents, format_msg(guild, channel, msg), send_time)

def format_text_reminder(reminder: Reminder) -> str:
    guild, channel, msg, send_time, contents = itemgetter("guild", "channel", "msg", "time", "contents")(reminder)
    time_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(send_time)))
    if contents == "": return '{} for {}'.format(format_msg(guild, channel, msg), time_str)
    return '"""{}""" ({}) for {}'.format(contents, format_msg(guild, channel, msg), time_str)

async def send_reminder(user_id: str, reminder: Reminder) -> None:
    channel = discord_client.client.get_channel(int(reminder["channel"]))
    if not isinstance(channel, discord.TextChannel):
        logger.info("Reminder {} for user {} silently removed (channel no longer exists)".format(str(reminder), user_id))
        return
    try:
        creation_time = discord.utils.snowflake_time(int(reminder["msg"])).replace(tzinfo=timezone.utc)
        user = int(user_id)
        await channel.send(util.discord.format("{!m} asked to be reminded <t:{}:R>: {}",
                user, int(creation_time.timestamp()), reminder["contents"])[:2000],
            reference = discord.MessageReference(message_id = int(reminder["msg"]),
                channel_id = int(reminder["channel"]), fail_if_not_exists = False),
            allowed_mentions=discord.AllowedMentions(everyone = False, users = [discord.Object(user)], roles = False))
    except discord.Forbidden:
        logger.info("Reminder {} for user {} silently removed (permission error)".format(str(reminder), user_id))

async def handle_reminder(user_id: str, reminder: Reminder) -> None:
    await send_reminder(user_id, reminder)

    reminders_optional = conf[user_id]
    if reminders_optional is None: return
    reminders = reminders_optional.copy()
    reminders.remove(reminder)
    conf[user_id] = FrozenList(reminders)

expiration_updated = asyncio.Semaphore(value=0)

async def expire_reminders() -> None:
    await discord_client.client.wait_until_ready()

    while True:
        try:
            now = datetime.now(timezone.utc).timestamp()
            next_expiry = None
            for user_id in conf:
                reminders = conf[user_id]
                if reminders is None: continue
                for reminder in reminders:
                    if int(reminder["time"]) < now:
                        logger.debug("Expiring reminder for user #{}".format(user_id))
                        await handle_reminder(user_id, reminder)
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

    expiry_task: asyncio.Task[None] = util.asyncio.run_async(expire_reminders)
    @plugins.finalizer
    def cancel_expiry() -> None:
        expiry_task.cancel()

@plugins.commands.command("remindme")
@plugins.privileges.priv("remind")
async def remindme_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    if msg.guild is None: return

    time_arg = get_time(args)
    if time_arg is None: return

    reminders_optional = conf[str(msg.author.id)]
    reminders = reminders_optional.copy() if reminders_optional is not None else []
    reminder_time = time_arg + int(datetime.now(timezone.utc).timestamp())
    reminder: Reminder = {"guild": str(msg.guild.id), "channel": str(msg.channel.id), "msg": str(msg.id),
        "time": str(reminder_time), "contents": args.get_rest()}
    reminders += [reminder]
    reminders.sort(key = lambda a: int(a["time"]))
    conf[str(msg.author.id)] = FrozenList(reminders)
    expiration_updated.release()

    await msg.channel.send("Created reminder {}".format(format_reminder(reminder))[:2000],
        allowed_mentions=discord.AllowedMentions.none())

@plugins.commands.command("reminder")
@plugins.privileges.priv("remind")
async def reminder_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    reminders_optional = conf[str(msg.author.id)]
    reminders = reminders_optional.copy() if reminders_optional is not None else []

    if cmd.text.lower() == "list":
        reminder_list_md = "Your reminders include:\n{}".format("\n".join("**{:d}.** Reminder {}"
            .format(i, format_reminder(reminder)) for i, reminder in zip(count(1), reminders)))
        if len(reminder_list_md) > 2000:
            await msg.channel.send(file = discord.File(io.BytesIO(
                "Your reminders include:\n{}".format("\n".join("{:d}. Reminder {}"
                    .format(i, format_text_reminder(reminder)) for i, reminder in zip(count(1), reminders))
                ).encode("utf8")), filename = "reminder_list.txt"))
        else:
            await msg.channel.send(reminder_list_md, allowed_mentions=discord.AllowedMentions.none())

    if cmd.text.lower() == "remove":
        remove_arg = args.next_arg()
        if not isinstance(remove_arg, plugins.commands.StringArg): return
        if not remove_arg.text.isdigit(): return
        reminder_remove = int(remove_arg.text)
        if reminder_remove < 1 or reminder_remove > len(reminders):
            await msg.channel.send("Reminder {:d} does not exist".format(reminder_remove))
            return
        reminder = reminders[reminder_remove - 1]
        del reminders[reminder_remove - 1]
        conf[str(msg.author.id)] = FrozenList(reminders)
        expiration_updated.release()
        await msg.channel.send("Removed reminder {}".format(format_reminder(reminder))[:2000],
            allowed_mentions=discord.AllowedMentions.none())
