"""
This module declares a decorator 'location' that can be put on a command to restrict its invocations to a particular set
of channels and categories
"""
import logging
from typing import Any, Awaitable, Callable, Coroutine, List, Literal, Optional, Protocol, Tuple, Union, cast

from discord import CategoryChannel, Thread
from discord.abc import GuildChannel
import discord.ext.commands

from bot.client import client
from bot.commands import Context, cleanup, group
from bot.privileges import priv
import plugins
import util.db.kv
from util.discord import PartialCategoryChannelConverter, PartialChannelConverter, UserError, format
from util.frozen_list import FrozenList

class LocationsConf(Awaitable[None], Protocol):
    def __getitem__(self, key: Tuple[str, Literal["channels", "categories"]]) -> Optional[FrozenList[int]]: ...
    def __setitem__(self, key: Tuple[str, Literal["channels", "categories"]],
        value: Optional[Union[List[int], FrozenList[int]]]) -> None: ...

conf: LocationsConf
logger: logging.Logger = logging.getLogger(__name__)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(LocationsConf, await util.db.kv.load(__name__))

def in_location(loc: str, channel: Union[GuildChannel, Thread]) -> bool:
    chans = conf[loc, "channels"]
    cats = conf[loc, "categories"]
    if chans and channel.id in chans:
        return True
    if chans and isinstance(channel, Thread) and channel.parent_id in chans:
        return True
    if cats and channel.category_id is not None and channel.category_id in cats:
        return True
    return False

def location(name: str) -> Callable[[Callable[..., Coroutine[Any, Any, None]]], Callable[
    ..., Coroutine[Any, Any, None]]]:
    """A decorator for a command that restricts it the location (set of channels and categories) with the given name."""
    def command_location_check(ctx: Context) -> bool:
        return isinstance(ctx.channel, (GuildChannel, Thread)) and in_location(name, ctx.channel)
    return discord.ext.commands.check(command_location_check)

class LocContext(Context):
    loc: str

@cleanup
@group("location")
@priv("shell")
async def location_command(ctx: Context) -> None:
    """Manage locations where a command can be invoked."""
    pass

def location_exists(loc: str) -> bool:
    return conf[loc, "channels"] is not None or conf[loc, "categories"] is not None

def validate_location(loc: str) -> None:
    if not location_exists(loc):
        raise UserError(format("Location {!i} does not exist", loc))

@location_command.command("new")
async def location_new(ctx: Context, loc: str) -> None:
    """Create a new location."""
    if location_exists(loc):
        raise UserError(format("Location {!i} already exists", loc))

    conf[loc, "channels"] = []
    conf[loc, "categories"] = []
    await conf

    await ctx.send(format("Created location {!i}", loc))

@location_command.command("delete")
async def location_delete(ctx: Context, loc: str) -> None:
    """Delete a location."""
    validate_location(loc)

    conf[loc, "channels"] = None
    conf[loc, "categories"] = None
    await conf

    await ctx.send(format("Removed location {!i}", loc))

@location_command.command("show")
async def location_show(ctx: Context, loc: str) -> None:
    """Show the channels and categories in a location."""
    validate_location(loc)
    chans = conf[loc, "channels"]
    cats = conf[loc, "categories"]
    output = []
    for id in chans or ():
        chan = client.get_channel(id)
        if isinstance(chan, GuildChannel):
            ctext = format("{!c}({!i} {!i})", chan, chan.name, chan.id)
        else:
            ctext = format("{!c}({!i})", id, id)
        output.append("channel {}".format(ctext))
    for id in cats or ():
        cat = client.get_channel(id)
        if isinstance(cat, CategoryChannel):
            ctext = format("{!c}({!i} {!i})", cat, cat.name, cat.id)
        else:
            ctext = format("{!c}({!i})", id, id)
        output.append("category {}".format(ctext))
    await ctx.send(format("Location {!i} includes: {}", loc, "; ".join(output)))

@location_command.group("add")
async def location_add(ctx: LocContext, loc: str) -> None:
    """Add a channel or category to a location."""
    validate_location(loc)
    ctx.loc = loc

@location_add.command("channel")
async def location_add_channel(ctx: LocContext, chan: PartialChannelConverter) -> None:
    """Add a channel to a location."""
    loc = ctx.loc
    chans = conf[loc, "channels"] or FrozenList()
    if chan.id in chans:
        raise UserError(format("Channel {!c} is already in location {!i}", chan.id, loc))

    conf[loc, "channels"] = chans + [chan.id]
    await conf

    await ctx.send(format("Added channel {!c} to location {!i}", chan.id, loc))

@location_add.command("category")
async def location_add_category(ctx: LocContext, cat: PartialCategoryChannelConverter) -> None:
    """Add a category to a location."""
    loc = ctx.loc
    cats = conf[loc, "categories"] or FrozenList()
    if cat.id in cats:
        raise UserError(format("Category {!c} is already in location {!i}", cat.id, loc))

    conf[loc, "categories"] = cats + [cat.id]
    await conf

    await ctx.send(format("Added category {!c} to location {!i}", cat.id, loc))

@location_command.group("remove")
async def location_remove(ctx: LocContext, loc: str) -> None:
    """Remove a channel or category from a location."""
    validate_location(loc)
    ctx.loc = loc

@location_remove.command("channel")
async def location_remove_channel(ctx: LocContext, chan: PartialChannelConverter) -> None:
    """Remove a channel from a location."""
    loc = ctx.loc
    chans = conf[loc, "channels"] or FrozenList()
    if chan.id not in chans:
        raise UserError(format("Channel {!c} is already not in location {!i}", chan.id, loc))

    conf[loc, "channels"] = chans.without(chan.id)
    await conf

    await ctx.send(format("Removed channel {!c} from location {!i}", chan.id, loc))

@location_remove.command("category")
async def location_remove_category(ctx: LocContext, cat: PartialCategoryChannelConverter) -> None:
    """Remove a category from a location."""
    loc = ctx.loc
    cats = conf[loc, "categories"] or FrozenList()
    if cat.id not in cats:
        raise UserError(format("Category {!c} is already not in location {!i}", cat.id, loc))

    conf[loc, "categories"] = cats.without(cat.id)
    await conf

    await ctx.send(format("Added category {!c} to location {!i}", cat.id, loc))
