import logging
from typing import List, Dict, Optional, Union, Iterable, Callable, Awaitable, Literal, Protocol, cast
import discord
import discord.utils
import util.db.kv
from util.frozen_dict import FrozenDict
from util.frozen_list import FrozenList
import util.discord
import plugins.commands
import plugins.privileges

ADict = Union[Dict[str, List[str]], FrozenDict[str, FrozenList[str]]]

class LocationsConf(Protocol):
    def __getitem__(self, priv: str) -> Optional[FrozenDict[str, FrozenList[str]]]: ...
    def __setitem__(self, priv: str, p: Optional[ADict]) -> None: ...

conf = cast(LocationsConf, util.db.kv.Config(__name__))
logger: logging.Logger = logging.getLogger(__name__)

def in_location(loc: str, channel: discord.abc.GuildChannel) -> bool:
    obj = conf[loc]
    if obj and "channels" in obj:
        if str(channel.id) in obj["channels"]:
            return True
    if obj and "categories" in obj:
        if channel.category_id != None:
            if str(channel.category_id) in obj["categories"]:
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
            if isinstance(msg.channel, discord.abc.GuildChannel):
                if in_location(name, msg.channel):
                    await fun(msg, arg)
        check.__name__ = fun.__name__
        return check
    return decorator

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
    if conf[loc] is not None:
        await msg.channel.send(util.discord.format("Location {!i} already exists", loc))
        return
    conf[loc] = {"channels": [], "categories": []}
    await msg.channel.send(util.discord.format("Created location {!i}", loc))

async def loc_delete(msg: discord.Message, loc: str) -> None:
    if conf[loc] == None:
        await msg.channel.send(util.discord.format("Location {!i} does not exist", loc))
        return
    conf[loc] = None
    await msg.channel.send(util.discord.format("Removed location {!i}", loc))

async def loc_show(msg: discord.Message, loc: str) -> None:
    obj = conf[loc]
    if obj is None:
        await msg.channel.send(util.discord.format("Location {!i} does not exist", loc))
        return
    output = []
    if "channels" in obj:
        for id in map(int, obj["channels"]):
            chan = discord.utils.find(lambda c: c.id == id, msg.guild.channels if msg.guild else ())
            if chan is not None:
                ctext = util.discord.format("{!c}({!i} {!i})", chan, chan.name, chan.id)
            else:
                ctext = util.discord.format("{!c}({!i})", id, id)
            output.append("channel {}".format(ctext))
    if "categories" in obj:
        for id in map(int, obj["categories"]):
            cat = discord.utils.find(lambda r: r.id == id, msg.guild.categories if msg.guild else ())
            if cat is not None:
                ctext = util.discord.format("{!c}({!i} {!i})", cat, cat.name, cat.id)
            else:
                ctext = util.discord.format("{!c}({!i})", id, id)
            output.append("category {}".format(ctext))
    await msg.channel.send(util.discord.format("Location {!i} includes: {}", loc, "; ".join(output)))

async def loc_add_chan(msg: discord.Message, loc: str, obj: FrozenDict[str, FrozenList[str]], chan_id: int) -> None:
    if str(chan_id) in obj.get("channels", []):
        await msg.channel.send(util.discord.format("Channel {!c} is already in location {!i}", chan_id, loc))
        return

    chans = obj.get("channels") or FrozenList()
    chans += [str(chan_id)]
    conf[loc] = obj | {"channels": chans}

    await msg.channel.send(util.discord.format("Added channel {!c} to location {!i}", chan_id, loc))

async def loc_add_cat(msg: discord.Message, loc: str, obj: FrozenDict[str, FrozenList[str]], cat_id: int) -> None:
    if str(cat_id) in obj.get("categories", []):
        await msg.channel.send(util.discord.format("Category {!c} is already in location {!i}", cat_id, loc))
        return

    cats = obj.get("categories") or FrozenList()
    conf[loc] = obj | {"categories": cats + [str(cat_id)]}

    await msg.channel.send(util.discord.format("Added category {!c} to location {!i}", cat_id, loc))

async def loc_remove_chan(msg: discord.Message, loc: str, obj: FrozenDict[str, FrozenList[str]], chan_id: int) -> None:
    if str(chan_id) not in obj.get("channels", []):
        await msg.channel.send(util.discord.format("Channel {!c} is already not in location {!i}", chan_id, loc))
        return

    chans = obj.get("channels") or FrozenList()
    chans = FrozenList(filter(lambda i: i != str(chan_id), chans))
    conf[loc] = obj | {"channels": chans}

    await msg.channel.send(util.discord.format("Removed channel {!c} from location {!i}", chan_id, loc))

async def loc_remove_cat(msg: discord.Message, loc: str, obj: FrozenDict[str, FrozenList[str]], cat_id: int) -> None:
    if str(cat_id) not in obj.get("categories", []):
        await msg.channel.send(util.discord.format("Category {!c} is already not in location {!i}", cat_id, loc))
        return

    cats = obj.get("categories") or FrozenList()
    cats = FrozenList(filter(lambda i: i != str(cat_id), cats))
    conf[loc] = obj | {"categories": cats}

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
        obj = conf[loc.text]
        if obj is None:
            await msg.channel.send(util.discord.format("Location {!i} does not exist", loc.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "channel":
            arg = args.next_arg()
            if arg is None: return
            chan_id = chan_id_from_arg(msg.guild, arg)
            if chan_id is None: return
            await loc_add_chan(msg, loc.text, obj, chan_id)

        elif cmd.text.lower() == "category":
            arg = args.next_arg()
            if arg is None: return
            cat_id = cat_id_from_arg(msg.guild, arg)
            if cat_id is None: return
            await loc_add_cat(msg, loc.text, obj, cat_id)

    elif cmd.text.lower() == "remove":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        obj = conf[loc.text]
        if obj is None:
            await msg.channel.send(util.discord.format("Location {!i} does not exist", loc.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "channel":
            arg = args.next_arg()
            if arg is None: return
            chan_id = chan_id_from_arg(msg.guild, arg)
            if chan_id is None: return
            await loc_remove_chan(msg, loc.text, obj, chan_id)

        elif cmd.text.lower() == "category":
            arg = args.next_arg()
            if arg is None: return
            cat_id = cat_id_from_arg(msg.guild, arg)
            if cat_id is None: return
            await loc_remove_cat(msg, loc.text, obj, cat_id)
