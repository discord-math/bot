import asyncio
import discord
import logging
import discord_client
import util.db.kv
import plugins

logger = logging.getLogger(__name__)
conf = util.db.kv.Config(__name__)

class ModMailClient(discord.Client):
    async def on_message(self, msg):
        if not msg.guild:
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
                await channel.send((header + text)[:2000],
                    allowed_mentions=mentions)
                await channel.send(text[2000 - len(header):] + footer,
                    allowed_mentions=mentions)
            else:
                await channel.send(header + text + footer,
                    allowed_mentions=mentions)
            await msg.add_reaction("\u2709")

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
