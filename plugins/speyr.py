import re
import discord
import util.discord
import util.db.kv

conf = util.db.kv.Config(__name__)

def rule_embed(n):
    if conf[str(n)] != None:
        try:
            color = int(conf.color or "0", base=0)
        except ValueError:
            color = 0
        embed = discord.Embed(color=color)
        embed.add_field(inline=False, name="From",
            value=util.discord.format("{!c}", int(conf.channel or 0)))
        embed.add_field(inline=False, name="Rule {}".format(n),
            value=conf[str(n)])
        return embed

async def respond(msg, embed):
    try:
        await msg.channel.send(embed=embed)
        await msg.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

@util.discord.event("message")
async def speyr_message(msg):
    try:
        if msg.guild.id != int(conf.guild or 0):
            return
    except ValueError:
        return

    if msg.content == "!r":
        try:
            color = int(conf.color or "0", base=0)
        except ValueError:
            color = 0
        embed = discord.Embed(color=color, title="#questions",
            description=util.discord.format(
                "For a list of rules about asking on this server, "
                "please see {!c}.", int(conf.channel or 0)))
        await respond(msg, embed)

    elif msg.content == "!15m":
        embed = rule_embed(conf.rule_15m)
        if embed:
            await respond(msg, embed)

    elif match := re.fullmatch(r"!r(\d+)", msg.content):
        embed = rule_embed(int(match[1]))
        if embed:
            await respond(msg, embed)
