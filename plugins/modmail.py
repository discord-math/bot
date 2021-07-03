import asyncio
import discord
import logging
from typing import Dict, Tuple, Optional, Any, Protocol, cast
import discord_client
import util.db
import util.db.kv
import plugins
import plugins.reactions

class ModmailConf(Protocol):
    token: str
    guild: str
    channel: str
    role: str
    thread_expiry: int

conf = cast(ModmailConf, util.db.kv.Config(__name__))
logger: logging.Logger = logging.getLogger(__name__)

@util.db.init
def db_init() -> str:
    return r"""
        CREATE SCHEMA modmail;
        CREATE TABLE modmail.messages
            ( dm_channel_id BIGINT NOT NULL
            , dm_message_id BIGINT NOT NULL
            , staff_message_id BIGINT PRIMARY KEY
            );
        CREATE TABLE modmail.threads
            ( user_id BIGINT NOT NULL
            , thread_first_message_id BIGINT NOT NULL
            , last_used TIMESTAMP NOT NULL
            );
        """

message_map: Dict[int, Tuple[int, int]] = {}

with util.db.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM modmail.messages")
        for dm_chan_id, dm_msg_id, msg_id in cur.fetchall():
            message_map[msg_id] = (dm_chan_id, dm_msg_id)

def add_modmail(source: discord.Message, copy: discord.Message) -> None:
    with util.db.connection() as conn:
        conn.cursor().execute("""
            INSERT INTO modmail.messages
                (dm_channel_id, dm_message_id, staff_message_id)
                VALUES (%s, %s, %s)
            """, (source.channel.id, source.id, copy.id))
        message_map[copy.id] = (source.channel.id, source.id)

def update_thread(user_id: int) -> Optional[int]:
    with util.db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE modmail.threads
                    SET last_used = CURRENT_TIMESTAMP
                    WHERE user_id = %s AND last_used > CURRENT_TIMESTAMP - %s * '1 second'::INTERVAL
                    RETURNING thread_first_message_id
                """, (user_id, conf.thread_expiry))
            row = cur.fetchone()
            return row[0] if row is not None else None

def create_thread(user_id: int, msg_id: int) -> None:
    with util.db.connection() as conn:
        conn.cursor().execute("""
            INSERT INTO modmail.threads
                (user_id, thread_first_message_id, last_used)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
            """, (user_id, msg_id))

class ModMailClient(discord.Client):
    async def on_ready(self) -> None:
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="DMs"))

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        logger.error("Exception in modmail client {}".format(event_method), exc_info=True)

    async def on_message(self, msg: discord.Message) -> None:
        if not msg.guild and msg.author.id != self.user.id:
            try:
                guild = discord_client.client.get_guild(int(conf.guild))
                if guild is None: return
                channel = guild.get_channel(int(conf.channel))
                if not isinstance(channel, discord.TextChannel): return
                role = guild.get_role(int(conf.role))
                if role is None: return
            except (ValueError, AttributeError):
                return
            thread_id = update_thread(msg.author.id)
            header = util.discord.format("**From {}#{}** {} {!m} on {}:\n\n",
                msg.author.name, msg.author.discriminator, msg.author.id, msg.author, msg.created_at)
            footer = "".join("\n**Attachment:** {} {}".format(att.filename, att.url) for att in msg.attachments)
            if thread_id is None:
                footer += util.discord.format("\n{!m}", role)
            if footer:
                footer = "\n" + footer
            text = msg.content
            mentions = discord.AllowedMentions.none()
            mentions.roles = [role]
            reference = None
            if thread_id is not None:
                reference = discord.MessageReference(
                    message_id=thread_id, channel_id=channel.id, fail_if_not_exists=False)
            copy_first = None
            sent_footer = False
            for i in range(0, len(header) + len(text), 2000):
                part = (header + text)[i:i + 2000]
                if len(part) + len(footer) <= 2000:
                    part += footer
                    sent_footer = True
                copy = await channel.send(part,
                    allowed_mentions=mentions, reference=reference if copy_first is None else None)
                add_modmail(msg, copy)
                if copy_first is None:
                    copy_first = copy
            if not sent_footer:
                copy = await channel.send(footer,
                    allowed_mentions=mentions)
                add_modmail(msg, copy)
            if thread_id is None and copy_first is not None:
                create_thread(msg.author.id, copy_first.id)
            await msg.add_reaction("\u2709")

@util.discord.event("message")
async def modmail_reply(msg: discord.Message) -> None:
    if msg.reference and msg.reference.message_id in message_map and not msg.author.bot:
        dm_chan_id, dm_msg_id = message_map[msg.reference.message_id]

        anon_react = "\U0001F574"
        named_react = "\U0001F9CD"
        cancel_react = "\u274C"

        try:
            query = await msg.channel.send(
                "Reply anonymously {}, personally {}, or cancel {}".format(anon_react, named_react, cancel_react))
        except (discord.NotFound, discord.Forbidden):
            return

        reactions = (anon_react, named_react, cancel_react)
        try:
            with plugins.reactions.ReactionMonitor(channel_id=query.channel.id, message_id=query.id,
                author_id=msg.author.id, event="add", filter=lambda _, p: p.emoji.name in reactions,
                timeout_each=120) as mon:
                for react in reactions:
                    await query.add_reaction(react)
                _, payload = await mon
                reaction = payload.emoji.name
        except (discord.NotFound, discord.Forbidden):
            return
        except asyncio.TimeoutError:
            reaction = cancel_react

        await query.delete()
        if reaction == cancel_react:
            await msg.channel.send("Cancelled")
        else:
            header = ""
            if reaction == named_react:
                name = isinstance(msg.author, discord.Member) and msg.author.nick or msg.author.name
                header = util.discord.format("**From {}** {!m}:\n\n", name, msg.author)
            try:
                chan = await client.fetch_channel(dm_chan_id)
                if not isinstance(chan, discord.DMChannel):
                    await msg.channel.send("Could not deliver DM")
                    return
                await chan.send(header + msg.content,
                    reference=discord.MessageReference(message_id=dm_msg_id, channel_id=dm_chan_id))
            except (discord.NotFound, discord.Forbidden):
                await msg.channel.send("Could not deliver DM")
            else:
                await msg.channel.send("Message delivered")

client: discord.Client = ModMailClient(
    loop=asyncio.get_event_loop(),
    max_messages=None,
    intents=discord.Intents(dm_messages=True),
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

async def run_modmail() -> None:
    try:
        await client.start(conf.token, reconnect=True)
    except asyncio.CancelledError:
        pass
    except:
        logger.error("Exception in modmail client task", exc_info=True)
    finally:
        await client.close()

bot_task: asyncio.Task[None] = util.asyncio.run_async(run_modmail)
@plugins.finalizer
def cancel_bot_task() -> None:
    bot_task.cancel()
