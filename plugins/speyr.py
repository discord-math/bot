import re
import discord
from typing import Optional, Protocol, cast
import plugins
import util.discord
import util.db.kv

class SpeyrConf(Protocol):
    guild: Optional[str]
    channel: Optional[str]
    color: Optional[str]
    rule_15m: int
    def __getitem__(self, key: str) -> str: ...

conf: SpeyrConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(SpeyrConf, await util.db.kv.load(__name__))

def rule_embed(n: int) -> Optional[discord.Embed]:
    if conf[str(n)] != None:
        try:
            color = int(conf.color or "0", base=0)
        except ValueError:
            color = 0
        embed = discord.Embed(color=color)
        embed.add_field(inline=False, name="From", value=util.discord.format("{!c}", int(conf.channel or 0)))
        embed.add_field(inline=False, name="Rule {}".format(n), value=conf[str(n)])
        return embed
    return None

async def respond(msg: discord.Message, embed: discord.Embed) -> None:
    try:
        await msg.channel.send(embed=embed)
        await msg.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

@util.discord.event("message")
async def speyr_message(msg: discord.Message) -> None:
    try:
        if not msg.guild or msg.guild.id != int(conf.guild or 0):
            return
    except ValueError:
        return
    embed: Optional[discord.Embed]

    if msg.content == "!r":
        try:
            color = int(conf.color or "0", base=0)
        except ValueError:
            color = 0
        embed = discord.Embed(color=color, title="#questions",
            description=util.discord.format("For a list of rules about asking on this server, please see {!c}.",
                int(conf.channel or 0)))
        await respond(msg, embed)

    elif msg.content == "!15m":
        embed = rule_embed(conf.rule_15m)
        if embed is not None:
            await respond(msg, embed)

    elif match := re.fullmatch(r"!r(\d+)", msg.content):
        embed = rule_embed(int(match[1]))
        if embed is not None:
            await respond(msg, embed)
