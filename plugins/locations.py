import logging
from typing import List, Optional, Union, Tuple, Iterator, Coroutine, Literal, Callable, Awaitable, Protocol, Any, cast
import discord
import discord.ext.commands
import discord.utils
import util.db.kv
from util.frozen_list import FrozenList
import discord_client
import util.discord
import plugins.commands
import plugins.privileges

class LocationsConf(Protocol, Awaitable[None]):
    def __getitem__(self, key: Tuple[str, Literal["channels", "categories"]]) -> Optional[FrozenList[int]]: ...
    def __setitem__(self, key: Tuple[str, Literal["channels", "categories"]],
        value: Optional[Union[List[int], FrozenList[int]]]) -> None: ...

conf: LocationsConf
logger: logging.Logger = logging.getLogger(__name__)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(LocationsConf, await util.db.kv.load(__name__))

def in_location(loc: str, channel: discord.abc.GuildChannel) -> bool:
    chans = conf[loc, "channels"]
    cats = conf[loc, "categories"]
    if chans and channel.id in chans:
        return True
    if cats and channel.category_id is not None and channel.category_id in cats:
        return True
    return False

def location(name: str) -> Callable[[Callable[[discord.Message, plugins.commands.ArgParser], Awaitable[None]]],
    Callable[[discord.Message, plugins.commands.ArgParser], Awaitable[None]]]:
    """
    Require that a command is only available in a given location. The decorator should be specified after
    plugins.commands.command.
    """
    def decorator(fun: Callable[[discord.Message, plugins.commands.ArgParser], Awaitable[None]]) -> Callable[
        [discord.Message, plugins.commands.ArgParser], Awaitable[None]]:
        async def check(msg: discord.Message, arg: plugins.commands.ArgParser) -> None:
            if isinstance(msg.channel, discord.abc.GuildChannel) and in_location(name, msg.channel):
                await fun(msg, arg)
        check.__name__ = fun.__name__
        return check
    return decorator

def location_ext(name: str) -> Callable[[Callable[..., Coroutine[Any, Any, None]]], Callable[
    ..., Coroutine[Any, Any, None]]]:
    def command_location_check(ctx: discord.ext.commands.Context) -> bool:
        return isinstance(ctx.channel, discord.abc.GuildChannel) and in_location(name, ctx.channel)
    return discord.ext.commands.check(command_location_check)

class LocContext(discord.ext.commands.Context):
    loc: str

@plugins.commands.command_ext("location", cls=discord.ext.commands.Group)
@plugins.privileges.priv_ext("shell")
async def location_command(ctx: discord.ext.commands.Context) -> None:
    """Manage locations where a command can be invoked"""
    pass

def location_exists(loc: str) -> bool:
    return conf[loc, "channels"] is not None or conf[loc, "categories"] is not None

def validate_location(loc: str) -> None:
    if not location_exists(loc):
        raise util.discord.UserError(util.discord.format("Location {!i} does not exist", loc))

@location_command.command("new")
async def location_new(ctx: discord.ext.commands.Context, loc: str) -> None:
    """Create a new location"""
    if location_exists(loc):
        raise util.discord.UserError(util.discord.format("Location {!i} already exists", loc))

    conf[loc, "channels"] = []
    conf[loc, "categories"] = []
    await conf

    await ctx.send(util.discord.format("Created location {!i}", loc))

@location_command.command("delete")
async def location_delete(ctx: discord.ext.commands.Context, loc: str) -> None:
    """Delete a location"""
    validate_location(loc)

    conf[loc, "channels"] = None
    conf[loc, "categories"] = None
    await conf

    await ctx.send(util.discord.format("Removed location {!i}", loc))

@location_command.command("show")
async def location_show(ctx: discord.ext.commands.Context, loc: str) -> None:
    """Show the channels and categories in a location"""
    validate_location(loc)
    chans = conf[loc, "channels"]
    cats = conf[loc, "categories"]
    output = []
    for id in chans or ():
        chan = discord_client.client.get_channel(id)
        if isinstance(chan, discord.abc.GuildChannel):
            ctext = util.discord.format("{!c}({!i} {!i})", chan, chan.name, chan.id)
        else:
            ctext = util.discord.format("{!c}({!i})", id, id)
        output.append("channel {}".format(ctext))
    for id in cats or ():
        cat = discord_client.client.get_channel(id)
        if isinstance(cat, discord.CategoryChannel):
            ctext = util.discord.format("{!c}({!i} {!i})", cat, cat.name, cat.id)
        else:
            ctext = util.discord.format("{!c}({!i})", id, id)
        output.append("category {}".format(ctext))
    await ctx.send(util.discord.format("Location {!i} includes: {}", loc, "; ".join(output)))

@location_command.group("add")
async def location_add(ctx: LocContext, loc: str) -> None:
    """Add a channel or category to a location"""
    validate_location(loc)
    ctx.loc = loc

@location_add.command("channel")
async def location_add_channel(ctx: LocContext, chan: util.discord.PartialTextChannelConverter) -> None:
    """Add a channel to a location"""
    loc = ctx.loc
    chans = conf[loc, "channels"] or FrozenList()
    if chan.id in chans:
        raise util.discord.UserError(util.discord.format("Channel {!c} is already in location {!i}", chan.id, loc))

    conf[loc, "channels"] = chans + [chan.id]
    await conf

    await ctx.send(util.discord.format("Added channel {!c} to location {!i}", chan.id, loc))

@location_add.command("category")
async def location_add_category(ctx: LocContext, cat: util.discord.PartialCategoryChannelConverter) -> None:
    """Add a category to a location"""
    loc = ctx.loc
    cats = conf[loc, "categories"] or FrozenList()
    if cat.id in cats:
        raise util.discord.UserError(util.discord.format("Category {!c} is already in location {!i}", cat.id, loc))

    conf[loc, "categories"] = cats + [cat.id]
    await conf

    await ctx.send(util.discord.format("Added category {!c} to location {!i}", cat.id, loc))

@location_command.group("remove")
async def location_remove(ctx: LocContext, loc: str) -> None:
    """Remove a channel or category from a location"""
    validate_location(loc)
    ctx.loc = loc

@location_remove.command("channel")
async def location_remove_channel(ctx: LocContext, chan: util.discord.PartialTextChannelConverter) -> None:
    """Remove a channel from a location"""
    loc = ctx.loc
    chans = conf[loc, "channels"] or FrozenList()
    if chan.id not in chans:
        raise util.discord.UserError(util.discord.format("Channel {!c} is already not in location {!i}", chan.id, loc))

    mchans = chans.copy()
    mchans.remove(chan.id)
    conf[loc, "channels"] = mchans

    await ctx.send(util.discord.format("Removed channel {!c} from location {!i}", chan.id, loc))

@location_remove.command("category")
async def location_remove_category(ctx: LocContext, cat: util.discord.PartialCategoryChannelConverter) -> None:
    """Remove a category from a location"""
    loc = ctx.loc
    cats = conf[loc, "categories"] or FrozenList()
    if cat.id not in cats:
        raise util.discord.UserError(util.discord.format("Category {!c} is already not in location {!i}", cat.id, loc))

    mcats = cats.copy()
    mcats.remove(cat.id)
    conf[loc, "categories"] = mcats

    await ctx.send(util.discord.format("Added category {!c} to location {!i}", cat.id, loc))
