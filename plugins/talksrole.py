import re
import discord
from typing import Optional, Awaitable, Protocol, cast
import plugins
import util.discord
import util.db.kv

class TalksConf(Protocol, Awaitable[None]):
    channel: Optional[int]
    role: Optional[int]
    regex: Optional[str]

conf: TalksConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(TalksConf, await util.db.kv.load(__name__))

    conf.channel = int(conf.channel) if conf.channel is not None else None
    conf.role = int(conf.role) if conf.role is not None else None
    await conf

@util.discord.event("message")
async def notification_message(msg: discord.Message) -> None:
    if msg.channel is None or msg.channel.id != conf.channel:
        return

    if (role_id := conf.role) is None:
        return
    if any(role.id == role_id for role in msg.role_mentions):
        return

    regex = str(conf.regex or "")
    if not regex:
        return

    if not re.search(regex, msg.content, re.IGNORECASE | re.DOTALL):
        return

    await msg.channel.send(util.discord.format("{!m} {!M}", msg.author, role_id),
        allowed_mentions=discord.AllowedMentions(roles=[discord.Object(role_id)]))
