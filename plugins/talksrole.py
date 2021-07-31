import re
import discord
from typing import Optional, Protocol, cast
import plugins
import util.discord
import util.db.kv

class TalksConf(Protocol):
    channel: Optional[str]
    role: Optional[str]
    regex: Optional[str]

conf: TalksConf

@plugins.init_async
async def init() -> None:
    global conf
    conf = cast(TalksConf, await util.db.kv.load(__name__))

@util.discord.event("message")
async def notification_message(msg: discord.Message) -> None:
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

    await msg.channel.send(util.discord.format("{!m} {!M}", msg.author, role_id),
        allowed_mentions=discord.AllowedMentions(roles=[discord.Object(role_id)]))
