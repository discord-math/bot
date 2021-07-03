import logging
from typing import List, Optional, Union, Tuple, Iterator, Coroutine, Literal, Callable, Awaitable, Protocol, Any, cast
import discord
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

def chan_id_from_arg(guild: Optional[discord.Guild], arg: plugins.commands.Arg) -> Optional[int]:
    if isinstance(arg, plugins.commands.ChannelArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    chan = util.discord.smart_find(arg.text, guild.channels if guild else ())
    if chan is None:
        raise util.discord.UserError("Multiple or no results for channel {!i}", arg.text)
    return chan.id

def cat_id_from_arg(guild: Optional[discord.Guild], arg: plugins.commands.Arg) -> Optional[int]:
    if isinstance(arg, plugins.commands.ChannelArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    cat = util.discord.smart_find(arg.text, guild.categories if guild else ())
    if cat is None:
        raise util.discord.UserError("Multiple or no results for category {!i}", arg.text)
    return cat.id

async def loc_new(msg: discord.Message, loc: str) -> None:
    if conf[loc, "channels"] is not None or conf[loc, "categories"] is not None:
        await msg.channel.send(util.discord.format("Location {!i} already exists", loc))
        return
    conf[loc, "channels"] = []
    conf[loc, "categories"] = []
    await conf
    await msg.channel.send(util.discord.format("Created location {!i}", loc))

async def loc_delete(msg: discord.Message, loc: str) -> None:
    if conf[loc, "channels"] is None or conf[loc, "categories"] is None:
        await msg.channel.send(util.discord.format("Location {!i} does not exist", loc))
        return
    conf[loc, "channels"] = None
    conf[loc, "categories"] = None
    await conf
    await msg.channel.send(util.discord.format("Removed location {!i}", loc))

async def loc_show(msg: discord.Message, loc: str) -> None:
    chans = conf[loc, "channels"]
    cats = conf[loc, "categories"]
    if chans is None and cats is None:
        await msg.channel.send(util.discord.format("Location {!i} does not exist", loc))
        return
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
    await msg.channel.send(util.discord.format("Location {!i} includes: {}", loc, "; ".join(output)))

async def loc_add_chan(msg: discord.Message, loc: str, chan_id: int) -> None:
    chans = conf[loc, "channels"] or FrozenList()
    if chan_id in chans:
        await msg.channel.send(util.discord.format("Channel {!c} is already in location {!i}", chan_id, loc))
        return

    conf[loc, "channels"] = chans + [chan_id]
    await conf

    await msg.channel.send(util.discord.format("Added channel {!c} to location {!i}", chan_id, loc))

async def loc_add_cat(msg: discord.Message, loc: str, cat_id: int) -> None:
    cats = conf[loc, "categories"] or FrozenList()
    if cat_id in cats:
        await msg.channel.send(util.discord.format("Category {!c} is already in location {!i}", cat_id, loc))
        return

    conf[loc, "categories"] = cats + [cat_id]
    await conf

    await msg.channel.send(util.discord.format("Added category {!c} to location {!i}", cat_id, loc))

async def loc_remove_chan(msg: discord.Message, loc: str, chan_id: int) -> None:
    chans = conf[loc, "channels"] or FrozenList()
    if chan_id not in chans:
        await msg.channel.send(util.discord.format("Channel {!c} is already not in location {!i}", chan_id, loc))
        return

    mchans = chans.copy()
    mchans.remove(chan_id)
    conf[loc, "channels"] = mchans
    await conf

    await msg.channel.send(util.discord.format("Removed channel {!c} from location {!i}", chan_id, loc))

async def loc_remove_cat(msg: discord.Message, loc: str, cat_id: int) -> None:
    cats = conf[loc, "categories"] or FrozenList()
    if cat_id in cats:
        await msg.channel.send(util.discord.format("Category {!c} is already not in location {!i}", cat_id, loc))
        return

    mcats = cats.copy()
    mcats.remove(cat_id)
    conf[loc, "categories"] = mcats
    await conf

    await msg.channel.send(util.discord.format("Removed category {!c} from location {!i}", cat_id, loc))

@plugins.commands.command("location")
@plugins.privileges.priv("shell")
async def location_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    if cmd.text.lower() == "new":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        await loc_new(msg, loc.text)

    elif cmd.text.lower() == "delete":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        await loc_delete(msg, loc.text)

    elif cmd.text.lower() == "show":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        await loc_show(msg, loc.text)

    elif cmd.text.lower() == "add":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        if conf[loc.text, "channels"] is None and conf[loc.text, "categories"] is None:
            await msg.channel.send(util.discord.format("Location {!i} does not exist", loc.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "channel":
            arg = args.next_arg()
            if arg is None: return
            chan_id = chan_id_from_arg(msg.guild, arg)
            if chan_id is None: return
            await loc_add_chan(msg, loc.text, chan_id)

        elif cmd.text.lower() == "category":
            arg = args.next_arg()
            if arg is None: return
            cat_id = cat_id_from_arg(msg.guild, arg)
            if cat_id is None: return
            await loc_add_cat(msg, loc.text, cat_id)

    elif cmd.text.lower() == "remove":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        if conf[loc.text, "channels"] is None and conf[loc.text, "categories"] is None:
            await msg.channel.send(util.discord.format("Location {!i} does not exist", loc.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "channel":
            arg = args.next_arg()
            if arg is None: return
            chan_id = chan_id_from_arg(msg.guild, arg)
            if chan_id is None: return
            await loc_remove_chan(msg, loc.text, chan_id)

        elif cmd.text.lower() == "category":
            arg = args.next_arg()
            if arg is None: return
            cat_id = cat_id_from_arg(msg.guild, arg)
            if cat_id is None: return
            await loc_remove_cat(msg, loc.text, cat_id)
