import asyncio
import discord
import logging
import discord_client
import util.db
import util.db.kv
import plugins
import plugins.reactions

logger = logging.getLogger(__name__)
conf = util.db.kv.Config(__name__)

@util.db.init
def db_init():
    return r"""
        CREATE TABLE modmails
            ( dm_channel_id BIGINT NOT NULL
            , dm_message_id BIGINT NOT NULL
            , staff_message_id BIGINT PRIMARY KEY
            );
        """

message_map = {}

with util.db.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM modmails")
        for dm_chan_id, dm_msg_id, msg_id in cur.fetchall():
            message_map[msg_id] = (dm_chan_id, dm_msg_id)

def add_modmail(source, copy):
    with util.db.connection() as conn:
        conn.cursor().execute("""
            INSERT INTO modmails
                (dm_channel_id, dm_message_id, staff_message_id)
                VALUES (%s, %s, %s)
            """, (source.channel.id, source.id, copy.id))
        message_map[copy.id] = (source.channel.id, source.id)
        conn.commit()

class ModMailClient(discord.Client):
    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="DMs"))

    async def on_error(self, method, *args, **kwargs):
        logger.error("Exception in modmail client {}".format(method),
            exc_info=True)

    async def on_message(self, msg):
        if not msg.guild and msg.author.id != self.user.id:
            try:
                guild = discord_client.client.get_guild(int(conf.guild))
                channel = guild.get_channel(int(conf.channel))
                role = guild.get_role(int(conf.role))
            except (ValueError, AttributeError):
                return
            header = util.discord.format("**From {}#{}** {} {!m} on {}:\n\n",
                msg.author.name, msg.author.discriminator,
                msg.author.id, msg.author, msg.created_at)
            footer = "\n".join("**Attachment:** {} {}".format(
                att.filename, att.url)
                for att in msg.attachments)
            if footer:
                footer += "\n"
            footer = util.discord.format("\n\n{}{!m}", footer, role)
            text = msg.content
            mentions = discord.AllowedMentions.none()
            mentions.roles = [role]
            if len(header) + len(text) + len(footer) > 2000:
                copy1 = await channel.send((header + text)[:2000],
                    allowed_mentions=mentions)
                copy2 = await channel.send(text[2000 - len(header):] + footer,
                    allowed_mentions=mentions)
                add_modmail(msg, copy1)
                add_modmail(msg, copy2)
            else:
                copy = await channel.send(header + text + footer,
                    allowed_mentions=mentions)
                add_modmail(msg, copy)
            await msg.add_reaction("\u2709")

@util.discord.event("message")
async def modmail_reply(msg):
    if msg.reference and msg.reference.message_id in message_map:
        dm_chan_id, dm_msg_id = message_map[msg.reference.message_id]

        anon_react = "\U0001F574"
        named_react = "\U0001F9CD"
        cancel_react = "\u274C"

        try:
            query = await msg.channel.send(
                "Reply anonymously {}, personally {}, or cancel {}".format(
                    anon_react, named_react, cancel_react))
        except (discord.NotFound, discord.Forbidden):
            return

        reactions = (anon_react, named_react, cancel_react)
        try:
            with plugins.reactions.ReactionMonitor(
                channel_id=query.channel.id, message_id=query.id,
                author_id=msg.author.id, event="add",
                filter=lambda _, p: p.emoji.name in reactions,
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
                header = util.discord.format("**From {}** {!m}:\n\n",
                    msg.author.nick or msg.author.name, msg.author)
            try:
                chan = await client.fetch_channel(dm_chan_id)
                await chan.send(header + msg.content,
                    reference=discord.MessageReference(
                        message_id=dm_msg_id, channel_id=dm_chan_id))
            except (discord.NotFound, discord.Forbidden):
                await msg.channel.send("Could not deliver DM")
            else:
                await msg.channel.send("Message delivered")

client = ModMailClient(
    loop=asyncio.get_event_loop(),
    max_messages=None,
    intents=discord.Intents(dm_messages=True),
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

async def run_modmail():
    try:
        await client.login(conf.token)
        await client.connect(reconnect=True)
    except asyncio.CancelledError:
        pass
    except:
        logger.error("Exception in modmail client task", exc_info=True)
    finally:
        await client.close()

bot_task = asyncio.create_task(run_modmail())
@plugins.finalizer
def cancel_bot_task():
    bot_task.cancel()
