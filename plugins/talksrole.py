import re
import discord
import util.discord
import util.db.kv

conf = util.db.kv.Config(__name__)

@util.discord.event("message")
async def notification_message(msg):
    try:
        if not msg.channel or msg.channel.id != int(conf.channel or 0):
            return
    except ValueError:
        return

    try:
        role_id = int(conf.role or 0)
        if any(role.id == role_id for role in msg.role_mentions):
            return
    except ValueError:
        return

    regex = str(conf.regex or "")
    if not regex:
        return

    if not re.search(regex, msg.content, re.IGNORECASE | re.DOTALL):
        return

    await msg.channel.send(
        util.discord.format("{!m} {!M}", msg.author, role_id),
        allowed_mentions=discord.AllowedMentions(
            roles=[util.discord.RoleById(role_id)]))
