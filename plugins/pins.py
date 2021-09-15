import asyncio
import re
import discord
from typing import Dict, Pattern, Optional
import plugins.commands
import plugins.privileges
import plugins.locations
import plugins.reactions
import discord_client
import util.discord

class AbortDueToUnpin(Exception):
    pass

class AbortDueToOtherPin(Exception):
    pass

unpin_requests: Dict[int, plugins.reactions.ReactionMonitor[discord.RawReactionActionEvent]] = {}

@plugins.commands.cleanup
@plugins.commands.command_ext("pin")
@plugins.privileges.priv_ext("pin")
@plugins.locations.location_ext("pin")
async def pin_command(ctx: discord.ext.commands.Context, message: Optional[util.discord.ReplyConverter]) -> None:
    """Pin a message."""
    to_pin = util.discord.partial_from_reply(message, ctx)
    if not isinstance(ctx.channel, discord.abc.GuildChannel) or ctx.guild is None:
        raise util.discord.UserError("Can only be used in a guild")
    guild = ctx.guild

    try:
        pin_msg_task = asyncio.create_task(
            discord_client.client.wait_for("message",
                check=lambda m: m.guild is not None and m.guild.id == guild.id
                and m.channel.id == ctx.channel.id
                and m.type == discord.MessageType.pins_add
                and m.reference is not None and m.reference.message_id == to_pin.id))

        while True:
            try:
                await to_pin.pin(reason=util.discord.format("Requested by {!m}", ctx.author))
                break
            except (discord.Forbidden, discord.NotFound):
                pin_msg_task.cancel()
                break
            except discord.HTTPException as exc:
                if exc.text == "Cannot execute action on a system message" or exc.text == "Unknown Message":
                    pin_msg_task.cancel()
                    break
                elif not exc.text.startswith("Maximum number of pins reached"):
                    raise
                pins = await ctx.channel.pins()

                oldest_pin = pins[-1]

                async with util.discord.TempMessage(ctx,
                    "No space in pins. Unpin or press \u267B to remove oldest") as confirm_msg:
                    await confirm_msg.add_reaction("\u267B")
                    await confirm_msg.add_reaction("\u274C")

                    with plugins.reactions.ReactionMonitor(guild_id=guild.id, channel_id=ctx.channel.id,
                        message_id=confirm_msg.id, author_id=ctx.author.id, event="add",
                        filter=lambda _, p: p.emoji.name in ["\u267B","\u274C"], timeout_each=60) as mon:
                        try:
                            if ctx.author.id in unpin_requests:
                                unpin_requests[ctx.author.id].cancel(AbortDueToOtherPin())
                            unpin_requests[ctx.author.id] = mon
                            _, p = await mon
                            if p.emoji.name == "\u267B":
                                await oldest_pin.unpin(reason=util.discord.format("Requested by {!m}", ctx.author))
                            else:
                                break
                        except AbortDueToUnpin:
                            del unpin_requests[ctx.author.id]
                        except (asyncio.TimeoutError, AbortDueToOtherPin):
                            pin_msg_task.cancel()
                            break
                        else:
                            del unpin_requests[ctx.author.id]
    finally:
        try:
            pin_msg = await asyncio.wait_for(pin_msg_task, timeout=60)
            plugins.commands.add_cleanup(ctx, pin_msg)
        except asyncio.TimeoutError:
            pin_msg_task.cancel()

@plugins.commands.cleanup
@plugins.commands.command_ext("unpin")
@plugins.privileges.priv_ext("pin")
@plugins.locations.location_ext("pin")
async def unpin_command(ctx: discord.ext.commands.Context, message: Optional[util.discord.ReplyConverter]) -> None:
    """Unpin a message."""
    to_unpin = util.discord.partial_from_reply(message, ctx)
    if not isinstance(ctx.channel, discord.abc.GuildChannel) or ctx.guild is None:
        raise util.discord.UserError("Can only be used in a guild")
    guild = ctx.guild

    try:
        await to_unpin.unpin(reason=util.discord.format("Requested by {!m}", ctx.author))
        if ctx.author.id in unpin_requests:
            unpin_requests[ctx.author.id].cancel(AbortDueToUnpin())

        await ctx.send("\u2705")
    except (discord.Forbidden, discord.NotFound):
        pass
    except discord.HTTPException as exc:
        if exc.text == "Cannot execute action on a system message":
            pass
        elif exc.text == "Unknown Message":
            pass
        else:
            raise
