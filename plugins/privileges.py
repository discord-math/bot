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

def user_id_from_arg(guild, arg):
    if isinstance(arg, plugins.commands.UserMentionArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    user = util.discord.smart_find(arg.text, guild.members if guild else ())
    if user == None:
        raise util.discord.UserError(
            "Multiple or no results for user {!i}", arg.text)
    return user.id

def role_id_from_arg(guild, arg):
    if isinstance(arg, plugins.commands.RoleMentionArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    role = util.discord.smart_find(arg.text, guild.roles if guild else ())
    if role == None:
        raise util.discord.UserError(
            "Multiple or no results for role {!i}", arg.text)
    return role.id

@plugins.commands.command("priv")
@priv("shell")
async def priv_command(msg, args):
    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    if cmd.text.lower() == "new":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        if conf[priv.text] != None:
            return await msg.channel.send(util.discord.format(
                "Priv {!i} already exists", priv.text))
        conf[priv.text] = {"users": [], "roles": []}
        await msg.channel.send(util.discord.format(
            "Created priv {!i}", priv.text))

    elif cmd.text.lower() == "delete":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        if conf[priv.text] == None:
            return await msg.channel.send(util.discord.format(
                "Priv {!i} does not exist", priv.text))
        conf[priv.text] = None
        await msg.channel.send(util.discord.format(
            "Removed priv {!i}", priv.text))

    elif cmd.text.lower() == "show":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        obj = conf[priv.text]
        if obj == None:
            await msg.channel.send(util.discord.format(
                "Priv {!i} does not exist", priv.text))
        output = []
        if "users" in obj:
            for id in obj["users"]:
                member = discord.utils.find(lambda m: m.id == id,
                    msg.guild.members if msg.guild else ())
                if member:
                    member = util.discord.format("{!m}({!i} {!i})",
                        member, member.name, member.id)
                else:
                    member = util.discord.format("{!m}({!i})", id, id)
                output.append("user {}".format(member))
        if "roles" in obj:
            for id in obj["roles"]:
                role = discord.utils.find(lambda r: r.id == id,
                    msg.guild.roles if msg.guild else ())
                if role:
                    role = util.discord.format("{!M}({!i} {!i})",
                        role, role.name, role.id)
                else:
                    role = util.discord.format("{!M}({!i})", id, id)
                output.append("role {}".format(role))
        await msg.channel.send(util.discord.format(
            "Priv {!i} includes: {}", priv.text, "; ".join(output)),
            allowed_mentions=discord.AllowedMentions.none())

    elif cmd.text.lower() == "add":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        obj = conf[priv.text]
        if obj == None:
            await msg.channel.send(util.discord.format(
                "Priv {!i} does not exist", priv.text))
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "user":
            user_id = user_id_from_arg(msg.guild, args.next_arg())
            if user_id == None: return
            if user_id in obj.get("users", []):
                return await msg.channel.send(util.discord.format(
                    "User {!m} is already in priv {!i}", user_id, priv.text),
                    allowed_mentions=discord.AllowedMentions.none())

            obj = dict(obj)
            obj["users"] = obj.get("users", []) + [user_id]
            conf[priv.text] = obj

            await msg.channel.send(util.discord.format(
                "Added user {!m} to priv {!i}", user_id, priv.text),
                allowed_mentions=discord.AllowedMentions.none())

        elif cmd.text.lower() == "role":
            role_id = role_id_from_arg(msg.guild, args.next_arg())
            if role_id == None: return
            if role_id in obj.get("roles", []):
                return await msg.channel.send(util.discord.format(
                    "Role {!M} is already in priv {!i}", role_id, priv.text),
                    allowed_mentions=discord.AllowedMentions.none())

            obj = dict(obj)
            obj["roles"] = obj.get("roles", []) + [role_id]
            conf[priv.text] = obj

            await msg.channel.send(util.discord.format(
                "Added role {!M} to priv {!i}", role_id, priv.text),
                allowed_mentions=discord.AllowedMentions.none())

    elif cmd.text.lower() == "remove":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        obj = conf[priv.text]
        if obj == None:
            await msg.channel.send(util.discord.format(
                "Priv {!i} does not exist", priv.text))
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "user":
            user_id = user_id_from_arg(msg.guild, args.next_arg())
            if user_id == None: return
            if user_id not in obj.get("users", []):
                return await msg.channel.send(util.discord.format(
                    "User {!m} is already not in priv {!i}",
                    user_id, priv.text),
                    allowed_mentions=discord.AllowedMentions.none())

            obj = dict(obj)
            obj["users"] = list(filter(lambda i: i != user_id,
                obj.get("users", [])))
            conf[priv.text] = obj

            await msg.channel.send(util.discord.format(
                "Removed user {!m} from priv {!i}", user_id, priv.text),
                allowed_mentions=discord.AllowedMentions.none())

        elif cmd.text.lower() == "role":
            role_id = role_id_from_arg(msg.guild, args.next_arg())
            if role_id == None: return
            if role_id not in obj.get("roles", []):
                return await msg.channel.send(util.discord.format(
                    "Role {!M} is already not in priv {!i}",
                    role_id, priv.text),
                    allowed_mentions=discord.AllowedMentions.none())

            obj = dict(obj)
            obj["roles"] = list(filter(lambda i: i != role_id,
                obj.get("roles", [])))
            conf[priv.text] = obj

            await msg.channel.send(util.discord.format(
                "Removed role {!M} from priv {!i}", role_id, priv.text),
                allowed_mentions=discord.AllowedMentions.none())
