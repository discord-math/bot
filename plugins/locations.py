import logging
import discord.utils
import util.db.kv
import util.discord
import plugins.commands
import plugins.privileges

logger = logging.getLogger(__name__)
conf = util.db.kv.Config(__name__)

def in_location(loc, channel):
    obj = conf[loc]
    if obj and "channels" in obj:
        if channel.id in obj["channels"]:
            return True
    if obj and "categories" in obj:
        if channel.category_id != None:
            if channel.category_id in obj["categories"]:
                return True
    return False

def location(name):
    """
    Require that a command is only available in a given location. The decorator
    should be specified after plugins.commands.command.
    """
    def decorator(fun):
        async def check(msg, arg):
            if in_location(name, msg.channel):
                await fun(msg, arg)
        return check
    return decorator

def chan_id_from_arg(guild, arg):
    if isinstance(arg, plugins.commands.ChannelArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    chan = util.discord.smart_find(arg.text, guild.channels if guild else ())
    if chan == None:
        raise util.discord.UserError(
            "Multiple or no results for channel {!i}", arg.text)
    return chan.id

def cat_id_from_arg(guild, arg):
    if isinstance(arg, plugins.commands.ChannelArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    cat = util.discord.smart_find(arg.text, guild.categories if guild else ())
    if cat == None:
        raise util.discord.UserError(
            "Multiple or no results for category {!i}", arg.text)
    return cat.id

@plugins.commands.command("location")
@plugins.privileges.priv("admin")
async def location_command(msg, args):
    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    if cmd.text.lower() == "new":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        if conf[loc.text] != None:
            return await msg.channel.send(util.discord.format(
                "Location {!i} already exists", loc.text))
        conf[loc.text] = {"channels": [], "categories": []}
        await msg.channel.send(util.discord.format(
            "Created location {!i}", loc.text))

    elif cmd.text.lower() == "delete":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        if conf[loc.text] == None:
            return await msg.channel.send(util.discord.format(
                "Location {!i} does not exist", loc.text))
        conf[loc.text] = None
        await msg.channel.send(util.discord.format(
            "Removed location {!i}", loc.text))

    elif cmd.text.lower() == "show":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        obj = conf[loc.text]
        if obj == None:
            await msg.channel.send(util.discord.format(
                "Location {!i} does not exist", loc.text))
        output = []
        if "channels" in obj:
            for id in obj["channels"]:
                chan = discord.utils.find(lambda c: c.id == id,
                    msg.guild.channels if msg.guild else ())
                if chan:
                    chan = "{}({})".format(chan.name, chan.id)
                else:
                    chan = "{}".format(id)
                output.append(util.discord.format("channel {!i}", chan))
        if "categories" in obj:
            for id in obj["categories"]:
                cat = discord.utils.find(lambda r: r.id == id,
                    msg.guild.categories if msg.guild else ())
                if cat:
                    cat = "{}({})".format(cat.name, cat.id)
                else:
                    cat = "{}".format(id)
                output.append(util.discord.format("category {!i}", cat))
        await msg.channel.send(util.discord.format(
            "Location {!i} includes: {}", loc.text, "; ".join(output)))

    elif cmd.text.lower() == "add":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        obj = conf[loc.text]
        if obj == None:
            await msg.channel.send(util.discord.format(
                "Location {!i} does not exist", loc.text))
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "channel":
            chan_id = chan_id_from_arg(msg.guild, args.next_arg())
            if chan_id == None: return
            if chan_id in obj.get("channels", []):
                return await msg.channel.send(util.discord.format(
                    "Channel {} is already in location {!i}", chan_id, loc.text))

            obj = dict(obj)
            obj["channels"] = obj.get("channels", []) + [chan_id]
            conf[loc.text] = obj

            await msg.channel.send(util.discord.format(
                "Added channel {} to location {!i}", chan_id, loc.text))

        elif cmd.text.lower() == "category":
            cat_id = cat_id_from_arg(msg.guild, args.next_arg())
            if cat_id == None: return
            if cat_id in obj.get("categories", []):
                return await msg.channel.send(util.discord.format(
                    "Category {} is already in location {!i}", cat_id, loc.text))

            obj = dict(obj)
            obj["categories"] = obj.get("categories", []) + [cat_id]
            conf[loc.text] = obj

            await msg.channel.send(util.discord.format(
                "Added category {} to location {!i}", cat_id, loc.text))

    elif cmd.text.lower() == "remove":
        loc = args.next_arg()
        if not isinstance(loc, plugins.commands.StringArg): return
        obj = conf[loc.text]
        if obj == None:
            await msg.channel.send(util.discord.format(
                "Location {!i} does not exist", loc.text))
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "channel":
            chan_id = chan_id_from_arg(msg.guild, args.next_arg())
            if chan_id == None: return
            if chan_id not in obj.get("channels", []):
                return await msg.channel.send(util.discord.format(
                    "Channel {} is already not in location {!i}", chan_id, loc.text))

            obj = dict(obj)
            obj["channels"] = list(filter(lambda i: i != chan_id,
                obj.get("channels", [])))
            conf[loc.text] = obj

            await msg.channel.send(util.discord.format(
                "Removed channel {} from location {!i}", chan_id, loc.text))

        elif cmd.text.lower() == "category":
            cat_id = cat_id_from_arg(msg.guild, args.next_arg())
            if cat_id == None: return
            if cat_id not in obj.get("categories", []):
                return await msg.channel.send(util.discord.format(
                    "Category {} is already not in location {!i}", cat_id, loc.text))

            obj = dict(obj)
            obj["categories"] = list(filter(lambda i: i != cat_id,
                obj.get("categories", [])))
            conf[loc.text] = obj

            await msg.channel.send(util.discord.format(
                "Removed category {} from location {!i}", cat_id, loc.text))
