import asyncio
import re
import discord
from typing import Dict, Pattern
import plugins.commands
import plugins.privileges
import plugins.locations
import plugins.reactions
import discord_client
import util.discord

msg_link_re: Pattern[str] = re.compile(r"https?://(?:\w*\.)?(?:discord.com|discordapp.com)/channels/(\d+)/(\d+)/(\d+)")
int_re: Pattern[str] = re.compile(r"\d+")

class AbortDueToUnpin(Exception):
    pass

class AbortDueToOtherPin(Exception):
    pass

unpin_requests: Dict[int, plugins.reactions.ReactionMonitor[discord.RawReactionActionEvent]] = {}

@plugins.commands.command("pin")
@plugins.privileges.priv("pin")
@plugins.locations.location("pin")
async def pin_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    if not isinstance(msg, discord.abc.GuildChannel) or msg.guild is None:
        return
    if msg.reference is not None:
        if msg.reference.guild_id != msg.guild.id: return
        if msg.reference.channel_id != msg.channel.id: return
        to_pin = msg.channel.get_partial_message(msg.reference.message_id)
    else:
        arg = args.next_arg()
        if not isinstance(arg, plugins.commands.StringArg): return
        if match := msg_link_re.match(arg.text):
            guild_id = int(match[1])
            chan_id = int(match[2])
            msg_id = int(match[3])
            if guild_id != msg.guild.id or chan_id != msg.channel.id: return
            to_pin = discord.PartialMessage(channel=msg.channel, id=msg_id)
        elif match := int_re.match(arg.text):
            msg_id = int(match[0])
            to_pin = discord.PartialMessage(channel=msg.channel, id=msg_id)
        else:
            return

    try:
        pin_msg_task = asyncio.create_task(
                discord_client.client.wait_for("message",
                    check=lambda m: m.guild.id == msg.guild.id
                    and m.channel.id == msg.channel.id
                    and m.type == discord.MessageType.pins_add
                    and m.reference and m.reference.message_id == to_pin.id,
                    timeout=60))
        cmd_delete_task = asyncio.create_task(
                discord_client.client.wait_for("raw_message_delete",
                    check=lambda m: m.guild_id == msg.guild.id
                    and m.channel_id == msg.channel.id
                    and m.message_id == msg.id,
                    timeout=300))

        while True:
            try:
                await to_pin.pin(reason=util.discord.format("Requested by {!m}", msg.author))
                break
            except (discord.Forbidden, discord.NotFound):
                break
            except discord.HTTPException as exc:
                if exc.text == "Cannot execute action on a system message":
                    break
                elif exc.text == "Unknown Message":
                    break
                elif not exc.text.startswith("Maximum number of pins reached"):
                    raise
                pins = await msg.channel.pins()

                oldest_pin = pins[-1]

                async with util.discord.TempMessage(msg.channel,
                    "No space in pins. Unpin or press \u267B to remove oldest") as confirm_msg:
                    await confirm_msg.add_reaction("\u267B")
                    await confirm_msg.add_reaction("\u274C")

                    with plugins.reactions.ReactionMonitor(guild_id=msg.guild.id, channel_id=msg.channel.id,
                        message_id=confirm_msg.id, author_id=msg.author.id, event="add",
                        filter=lambda _, p: p.emoji.name in ["\u267B","\u274C"], timeout_each=60) as mon:
                        try:
                            if msg.author.id in unpin_requests:
                                unpin_requests[msg.author.id].cancel(AbortDueToOtherPin())
                            unpin_requests[msg.author.id] = mon
                            _, p = await mon
                            if p.emoji.name == "\u267B":
                                await oldest_pin.unpin(reason=util.discord.format("Requested by {!m}", msg.author))
                            else:
                                break
                        except AbortDueToUnpin:
                            pass
                        except (asyncio.TimeoutError, AbortDueToOtherPin):
                            break
                        finally:
                            del unpin_requests[msg.author.id]
    finally:
        async def cleanup():
            try:
                pin_msg = await pin_msg_task
                await cmd_delete_task
                await pin_msg.delete()
            except (asyncio.TimeoutError, discord.Forbidden, discord.NotFound):
                pin_msg_task.cancel()
                cmd_delete_task.cancel()
        asyncio.create_task(cleanup())

@plugins.commands.command("unpin")
@plugins.privileges.priv("pin")
@plugins.locations.location("pin")
async def unpin_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    if not isinstance(msg, discord.abc.GuildChannel) or msg.guild is None:
        return
    if msg.reference != None:
        if msg.reference.guild_id != msg.guild.id: return
        if msg.reference.channel_id != msg.channel.id: return
        to_unpin = discord.PartialMessage(channel=msg.channel, id=msg.reference.message_id)
    else:
        arg = args.next_arg()
        if not isinstance(arg, plugins.commands.StringArg): return
        if match := msg_link_re.match(arg.text):
            guild_id = int(match[1])
            chan_id = int(match[2])
            msg_id = int(match[3])
            if guild_id != msg.guild.id or chan_id != msg.channel.id: return
            to_unpin = discord.PartialMessage(channel=msg.channel, id=msg_id)
        elif match := int_re.match(arg.text):
            msg_id = int(match[0])
            to_unpin = discord.PartialMessage(channel=msg.channel, id=msg_id)
        else:
            return

    try:
        confirm_msg = None
        cmd_delete_task = asyncio.create_task(
                discord_client.client.wait_for("raw_message_delete",
                    check=lambda m: m.guild_id == msg.guild.id
                    and m.channel_id == msg.channel.id
                    and m.message_id == msg.id,
                    timeout=300))

        try:
            await to_unpin.unpin(reason=util.discord.format("Requested by {!m}", msg.author))
            if msg.author.id in unpin_requests:
                unpin_requests[msg.author.id].cancel(AbortDueToUnpin())

            confirm_msg = await msg.channel.send("\u2705")
        except (discord.Forbidden, discord.NotFound):
            pass
        except discord.HTTPException as exc:
            if exc.text == "Cannot execute action on a system message":
                pass
            elif exc.text == "Unknown Message":
                pass
            else:
                raise
    finally:
        async def cleanup() -> None:
            try:
                await cmd_delete_task
                if confirm_msg:
                    await confirm_msg.delete()
            except (asyncio.TimeoutError, discord.Forbidden, discord.NotFound):
                pass
        asyncio.create_task(cleanup())
