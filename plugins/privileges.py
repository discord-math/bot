import logging
from typing import List, Dict, Optional, Union, Iterable, Callable, Awaitable, Literal, Protocol, cast
import discord
import discord.utils
import util.db.kv
from util.frozen_dict import FrozenDict
from util.frozen_list import FrozenList
import util.discord
import plugins.commands

ADict = Union[Dict[str, List[str]], FrozenDict[str, FrozenList[str]]]

class PrivilegesConf(Protocol):
    def __getitem__(self, priv: str) -> Optional[FrozenDict[str, FrozenList[str]]]: ...
    def __setitem__(self, priv: str, p: Optional[ADict]) -> None: ...

conf = cast(PrivilegesConf, util.db.kv.Config(__name__))
logger: logging.Logger = logging.getLogger(__name__)

def has_privilege(priv: str, user_or_member: Union[discord.User, discord.Member]) -> bool:
    obj = conf[priv]
    if obj and "users" in obj:
        if str(user_or_member.id) in obj["users"]:
            return True
    if obj and "roles" in obj:
        if isinstance(user_or_member, discord.Member):
            for role in user_or_member.roles:
                if str(role.id) in obj["roles"]:
                    return True
        # else we're in a DM or the user has left,
        # either way there's no roles to check
    return False

def priv(name: str) -> Callable[[Callable[[discord.Message, plugins.commands.ArgParser], Awaitable[None]]],
    Callable[[discord.Message, plugins.commands.ArgParser], Awaitable[None]]]:
    """
    Require that a command is only available to a given privilege. The decorator should be specified after
    plugins.commands.command.
    """
    def decorator(fun: Callable[[discord.Message, plugins.commands.ArgParser], Awaitable[None]]) -> Callable[
        [discord.Message, plugins.commands.ArgParser], Awaitable[None]]:
        async def check(msg: discord.Message, arg: plugins.commands.ArgParser) -> None:
            if has_privilege(name, msg.author):
                await fun(msg, arg)
            else:
                logger.warn("Denied {} to {!r}".format(fun.__name__, msg.author))
        check.__name__ = fun.__name__
        return check
    return decorator

def user_id_from_arg(guild: Optional[discord.Guild], arg: plugins.commands.Arg) -> Optional[int]:
    if isinstance(arg, plugins.commands.UserMentionArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    user = util.discord.smart_find(arg.text, guild.members if guild else ())
    if user is None:
        raise util.discord.UserError("Multiple or no results for user {!i}", arg.text)
    return user.id

def role_id_from_arg(guild: Optional[discord.Guild], arg: plugins.commands.Arg) -> Optional[int]:
    if isinstance(arg, plugins.commands.RoleMentionArg):
        return arg.id
    if not isinstance(arg, plugins.commands.StringArg): return None
    role = util.discord.smart_find(arg.text, guild.roles if guild else ())
    if role is None:
        raise util.discord.UserError("Multiple or no results for role {!i}", arg.text)
    return role.id

async def priv_new(msg: discord.Message, priv: str) -> None:
    if conf[priv] is not None:
        await msg.channel.send(util.discord.format("Priv {!i} already exists", priv))
        return
    conf[priv] = {"users": [], "roles": []}
    await msg.channel.send(util.discord.format("Created priv {!i}", priv))

async def priv_delete(msg: discord.Message, priv: str) -> None:
    if conf[priv] == None:
        await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv))
        return
    conf[priv] = None
    await msg.channel.send(util.discord.format("Removed priv {!i}", priv))

async def priv_show(msg: discord.Message, priv: str) -> None:
    obj = conf[priv]
    if obj is None:
        await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv))
        return
    output = []
    if "users" in obj:
        for id in map(int, obj["users"]):
            member = discord.utils.find(lambda m: m.id == id, msg.guild.members if msg.guild else ())
            if member:
                mtext = util.discord.format("{!m}({!i} {!i})", member, member.name, member.id)
            else:
                mtext = util.discord.format("{!m}({!i})", id, id)
            output.append("user {}".format(mtext))
    if "roles" in obj:
        for id in map(int, obj["roles"]):
            role = discord.utils.find(lambda r: r.id == id, msg.guild.roles if msg.guild else ())
            if role:
                rtext = util.discord.format("{!M}({!i} {!i})", role, role.name, role.id)
            else:
                rtext = util.discord.format("{!M}({!i})", id, id)
            output.append("role {}".format(rtext))
    await msg.channel.send(util.discord.format("Priv {!i} includes: {}", priv, "; ".join(output)),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_add_user(msg: discord.Message, priv: str, obj: FrozenDict[str, FrozenList[str]], user_id: int) -> None:
    if str(user_id) in obj.get("users", []):
        await msg.channel.send(util.discord.format("User {!m} is already in priv {!i}", user_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    users = obj.get("users") or FrozenList()
    users += [str(user_id)]
    conf[priv] = obj | {"users": users}

    await msg.channel.send(util.discord.format("Added user {!m} to priv {!i}", user_id, priv),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_add_role(msg: discord.Message, priv: str, obj: FrozenDict[str, FrozenList[str]], role_id: int) -> None:
    if str(role_id) in obj.get("roles", []):
        await msg.channel.send(util.discord.format("Role {!M} is already in priv {!i}", role_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    roles = obj.get("roles") or FrozenList()
    conf[priv] = obj | {"roles": roles + [str(role_id)]}

    await msg.channel.send(util.discord.format("Added role {!M} to priv {!i}", role_id, priv),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_remove_user(msg: discord.Message, priv: str, obj: FrozenDict[str, FrozenList[str]], user_id: int
    ) -> None:
    if str(user_id) not in obj.get("users", []):
        await msg.channel.send(util.discord.format("User {!m} is already not in priv {!i}",
            user_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    users = obj.get("users") or FrozenList()
    users = FrozenList(filter(lambda i: i != str(user_id), users))
    conf[priv] = obj | {"users": users}

    await msg.channel.send(util.discord.format("Removed user {!m} from priv {!i}", user_id, priv),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_remove_role(msg: discord.Message, priv: str, obj: FrozenDict[str, FrozenList[str]], role_id: int
    ) -> None:
    if str(role_id) not in obj.get("roles", []):
        await msg.channel.send(util.discord.format("Role {!M} is already not in priv {!i}", role_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    roles = obj.get("roles") or FrozenList()
    roles = FrozenList(filter(lambda i: i != str(role_id), roles))
    conf[priv] = obj | {"roles": roles}

    await msg.channel.send(util.discord.format( "Removed role {!M} from priv {!i}", role_id, priv),
        allowed_mentions=discord.AllowedMentions.none())

@plugins.commands.command("priv")
@priv("shell")
async def priv_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    if cmd.text.lower() == "new":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        await priv_new(msg, priv.text)

    elif cmd.text.lower() == "delete":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        await priv_delete(msg, priv.text)

    elif cmd.text.lower() == "show":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        await priv_show(msg, priv.text)

    elif cmd.text.lower() == "add":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        obj = conf[priv.text]
        if obj is None:
            await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "user":
            arg = args.next_arg()
            if arg is None: return
            user_id = user_id_from_arg(msg.guild, arg)
            if user_id is None: return
            await priv_add_user(msg, priv.text, obj, user_id)

        elif cmd.text.lower() == "role":
            arg = args.next_arg()
            if arg is None: return
            role_id = role_id_from_arg(msg.guild, arg)
            if role_id is None: return
            await priv_add_role(msg, priv.text, obj, role_id)

    elif cmd.text.lower() == "remove":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        obj = conf[priv.text]
        if obj is None:
            await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "user":
            arg = args.next_arg()
            if arg is None: return
            user_id = user_id_from_arg(msg.guild, arg)
            if user_id is None: return
            await priv_remove_user(msg, priv.text, obj, user_id)

        elif cmd.text.lower() == "role":
            arg = args.next_arg()
            if arg is None: return
            role_id = role_id_from_arg(msg.guild, arg)
            if role_id is None: return
            await priv_remove_role(msg, priv.text, obj, role_id)
