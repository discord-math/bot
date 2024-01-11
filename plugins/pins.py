import asyncio
from typing import Dict, Optional

import discord
from discord import MessageType, RawReactionActionEvent
from discord.ext.commands import command

from bot.acl import privileged
from bot.client import client
from bot.commands import Context, add_cleanup, cleanup, plugin_command
from bot.reactions import ReactionMonitor
from util.discord import ReplyConverter, TempMessage, UserError, format, partial_from_reply

class AbortDueToUnpin(Exception):
    pass

class AbortDueToOtherPin(Exception):
    pass

unpin_requests: Dict[int, ReactionMonitor[RawReactionActionEvent]] = {}

@plugin_command
@cleanup
@command("pin")
@privileged
async def pin_command(ctx: Context, message: Optional[ReplyConverter]) -> None:
    """Pin a message."""
    to_pin = partial_from_reply(message, ctx)
    if ctx.guild is None:
        raise UserError("Can only be used in a guild")
    guild = ctx.guild

    pin_msg_task = asyncio.create_task(
        client.wait_for("message",
            check=lambda m: m.guild is not None and m.guild.id == guild.id
            and m.channel.id == ctx.channel.id
            and m.type == MessageType.pins_add
            and m.reference is not None and m.reference.message_id == to_pin.id))
    try:
        while True:
            try:
                await to_pin.pin(reason=format("Requested by {!m}", ctx.author))
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

                async with TempMessage(ctx,
                    "No space in pins. Unpin or press \u267B to remove oldest") as confirm_msg:
                    await confirm_msg.add_reaction("\u267B")
                    await confirm_msg.add_reaction("\u274C")

                    with ReactionMonitor(guild_id=guild.id, channel_id=ctx.channel.id,
                        message_id=confirm_msg.id, author_id=ctx.author.id, event="add",
                        filter=lambda _, p: p.emoji.name in ["\u267B","\u274C"], timeout_each=60) as mon:
                        try:
                            if ctx.author.id in unpin_requests:
                                unpin_requests[ctx.author.id].cancel(AbortDueToOtherPin())
                            unpin_requests[ctx.author.id] = mon
                            _, p = await mon
                            if p.emoji.name == "\u267B":
                                await oldest_pin.unpin(reason=format("Requested by {!m}", ctx.author))
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
            add_cleanup(ctx, pin_msg)
        except asyncio.TimeoutError:
            pin_msg_task.cancel()

@plugin_command
@cleanup
@command("unpin")
@privileged
async def unpin_command(ctx: Context, message: Optional[ReplyConverter]) -> None:
    """Unpin a message."""
    to_unpin = partial_from_reply(message, ctx)
    if ctx.guild is None:
        raise UserError("Can only be used in a guild")

    try:
        await to_unpin.unpin(reason=format("Requested by {!m}", ctx.author))
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
