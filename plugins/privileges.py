import logging
import discord.utils
import util.db.kv
import util.discord
import plugins.commands

logger = logging.getLogger(__name__)
conf = util.db.kv.Config(__name__)

def has_privilege(priv, user_or_member):
    obj = conf[priv]
    if obj and "users" in obj:
        if user_or_member.id in obj["users"]:
            return True
    if obj and "roles" in obj:
        if hasattr(user_or_member, "roles"):
            for role in user_or_member.roles:
                if role.id in obj["roles"]:
                    return True
        # else we're in a DM or the user has left,
        # either way there's no roles to check
    return False

def priv(name):
    """
    Require that a command is only available to a given privilege. The decorator
    should be specified after plugins.commands.command.
    """
    def decorator(fun):
        async def check(msg, arg):
            if has_privilege(name, msg.author):
                await fun(msg, arg)
            else:
                logger.warn(
                    "Denied {} to {!r}".format(fun.__name__, msg.author))
        return check
    return decorator

@plugins.commands.command("priv")
@priv("admin")
async def priv_command(msg, args):
    cmd = args.get_string_arg()

    if cmd == "new":
        priv = args.get_string_arg()
        if priv == None: return
        if conf[priv] != None:
            return await msg.channel.send(
                "Priv {} already exists".format(util.discord.Inline(priv)))
        conf[priv] = {"users": [], "roles": []}
        await msg.channel.send(
            "Created priv {}".format(util.discord.Inline(priv)))

    elif cmd == "delete":
        priv = args.get_string_arg()
        if priv == None: return
        if conf[priv] == None:
            return await msg.channel.send(
                "Priv {} does not exist".format(util.discord.Inline(priv)))
        conf[priv] = None
        await msg.channel.send(
            "Removed priv {}".format(util.discord.Inline(priv)))

    elif cmd == "show":
        priv = args.get_string_arg()
        if priv == None: return
        obj = conf[priv]
        if obj == None:
            await msg.channel.send(
                "Priv {} does not exist".format(util.discord.Inline(priv)))
        output = []
        if "users" in obj:
            for id in obj["users"]:
                member = discord.utils.find(
                    lambda m: m.id == id, msg.guild.members)
                if member:
                    member = "{}#{}({})".format(
                        member.nick or member.name,
                        member.discriminator, member.id)
                else:
                    member = "{}".format(id)
                output.append("user {}".format(util.discord.Inline(member)))
        if "roles" in obj:
            for id in obj["roles"]:
                role = discord.utils.find(
                    lambda r: r.id == id, msg.guild.roles)
                if role:
                    role = "{}({})".format(role.name, role.id)
                else:
                    role = "{}".format(id)
                output.append("role {}".format(util.discord.Inline(role)))
        await msg.channel.send(
            "Priv {} includes: {}".format(util.discord.Inline(priv),
                "; ".join(output)))

    elif cmd == "add":
        priv = args.get_string_arg()
        if priv == None: return
        obj = conf[priv]
        if obj == None:
            await msg.channel.send(
                "Priv {} does not exist".format(util.discord.Inline(priv)))
        cmd = args.get_string_arg()
        if cmd == "user":
            name = args.get_arg()
            if isinstance(name, plugins.commands.UserMentionArg):
                user_id = name.id
            else:
                if isinstance(name, plugins.commands.BracketedArg):
                    name = name.contents
                if not isinstance(name, str): return
                user = util.discord.smart_find(name, msg.guild.members)
                if user == None:
                    return await msg.channel.send(
                        "Multiple or no results for user {}".format(
                            util.discord.Inline(name)))
                user_id = user.id

            if user_id in obj.get("users", []):
                return await msg.channel.send(
                    "User {} is already in priv {}".format(user_id,
                        util.discord.Inline(priv)))

            obj = dict(obj)
            obj["users"] = obj.get("users", []) + [user_id]
            conf[priv] = obj

            await msg.channel.send(
                "Added user {} to priv {}".format(user_id,
                    util.discord.Inline(priv)))

        elif cmd == "role":
            name = args.get_arg()
            if isinstance(name, plugins.commands.RoleMentionArg):
                role_id = name.id
            else:
                if isinstance(name, plugins.commands.BracketedArg):
                    name = name.contents
                if not isinstance(name, str): return
                role = util.discord.smart_find(name, msg.guild.roles)
                if role == None:
                    return await msg.channel.send(
                        "Multiple or no results for role {}".format(
                            util.discord.Inline(name)))
                role_id = role.id

            if role_id in obj.get("roles", []):
                return await msg.channel.send(
                    "Role {} is already in priv {}".format(role_id,
                        util.discord.Inline(priv)))

            obj = dict(obj)
            obj["roles"] = obj.get("roles", []) + [role_id]
            conf[priv] = obj

            await msg.channel.send(
                "Added role {} to priv {}".format(role_id,
                    util.discord.Inline(priv)))

    elif cmd == "remove":
        priv = args.get_string_arg()
        if priv == None: return
        obj = conf[priv]
        if obj == None:
            await msg.channel.send(
                "Priv {} does not exist".format(util.discord.Inline(priv)))
        cmd = args.get_string_arg()
        if cmd == "user":
            name = args.get_arg()
            if isinstance(name, plugins.commands.UserMentionArg):
                user_id = name.id
            else:
                if isinstance(name, plugins.commands.BracketedArg):
                    name = name.contents
                if not isinstance(name, str): return
                user = util.discord.smart_find(name, msg.guild.members)
                if user == None:
                    return await msg.channel.send(
                        "Multiple or no results for user {}".format(
                            util.discord.Inline(name)))
                user_id = user.id

            if user_id not in obj.get("users", []):
                return await msg.channel.send(
                    "User {} is already not in priv {}".format(user_id,
                        util.discord.Inline(priv)))

            obj = dict(obj)
            obj["users"] = list(filter(lambda i: i != user_id,
                obj.get("users", [])))
            conf[priv] = obj

            await msg.channel.send(
                "Removed user {} from priv {}".format(user_id,
                    util.discord.Inline(priv)))

        elif cmd == "role":
            name = args.get_arg()
            if isinstance(name, plugins.commands.RoleMentionArg):
                role_id = name.id
            else:
                if isinstance(name, plugins.commands.BracketedArg):
                    name = name.contents
                if not isinstance(name, str): return
                role = util.discord.smart_find(name, msg.guild.roles)
                if role == None:
                    return await msg.channel.send(
                        "Multiple or no results for role {}".format(
                            util.discord.Inline(name)))
                role_id = role.id

            if role_id not in obj.get("roles", []):
                return await msg.channel.send(
                    "Role {} is already not in priv {}".format(role_id,
                        util.discord.Inline(priv)))

            obj = dict(obj)
            obj["roles"] = list(filter(lambda i: i != role_id,
                obj.get("roles", [])))
            conf[priv] = obj

            await msg.channel.send(
                "Removed role {} from priv {}".format(role_id,
                    util.discord.Inline(priv)))
