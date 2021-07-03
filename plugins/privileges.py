import logging
from typing import List, Optional, Union, Tuple, Iterator, Coroutine, Literal, Callable, Awaitable, Protocol, Any, cast
import discord
import discord.utils
import util.db.kv
from util.frozen_list import FrozenList
import discord_client
import util.discord
import plugins.commands

class PrivilegesConf(Protocol, Awaitable[None]):
    def __getitem__(self, key: Tuple[str, Literal["users", "roles"]]) -> Optional[FrozenList[int]]: ...
    def __setitem__(self, key: Tuple[str, Literal["users", "roles"]],
        value: Optional[Union[List[int], FrozenList[int]]]) -> None: ...

conf: PrivilegesConf
logger: logging.Logger = logging.getLogger(__name__)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(PrivilegesConf, await util.db.kv.load(__name__))

def has_privilege(priv: str, user_or_member: Union[discord.User, discord.Member]) -> bool:
    users = conf[priv, "users"]
    roles = conf[priv, "roles"]
    if users and user_or_member.id in users:
        return True
    if roles and isinstance(user_or_member, discord.Member):
        for role in user_or_member.roles:
            if role.id in roles:
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

def priv_ext(name: str) -> Callable[[Callable[..., Coroutine[Any, Any, None]]], Callable[
    ..., Coroutine[Any, Any, None]]]:
    def command_priv_check(ctx: discord.ext.commands.Context) -> bool:
        if has_privilege(name, ctx.author):
            return True
        else:
            logger.warn("Denied {} to {!r}".format(ctx.invoked_with, ctx.author))
            return False
    return discord.ext.commands.check(command_priv_check)

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
    if conf[priv, "users"] is not None or conf[priv, "roles"] is not None:
        await msg.channel.send(util.discord.format("Priv {!i} already exists", priv))
        return
    conf[priv, "users"] = []
    conf[priv, "roles"] = []
    await conf
    await msg.channel.send(util.discord.format("Created priv {!i}", priv))

async def priv_delete(msg: discord.Message, priv: str) -> None:
    if conf[priv, "users"] is None and conf[priv, "roles"] is None:
        await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv))
        return
    conf[priv, "users"] = None
    conf[priv, "roles"] = None
    await conf
    await msg.channel.send(util.discord.format("Removed priv {!i}", priv))

async def priv_show(msg: discord.Message, priv: str) -> None:
    users = conf[priv, "users"]
    roles = conf[priv, "roles"]
    if users is None and roles is None:
        await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv))
        return
    output = []
    for id in users or ():
        user = await discord_client.client.fetch_user(id)
        if user is not None:
            mtext = util.discord.format("{!m}({!i} {!i})", user, user.name, user.id)
        else:
            mtext = util.discord.format("{!m}({!i})", id, id)
        output.append("user {}".format(mtext))
    for id in roles or ():
        role = discord.utils.find(lambda r: r.id == id, msg.guild.roles if msg.guild else ())
        if role is not None:
            rtext = util.discord.format("{!M}({!i} {!i})", role, role.name, role.id)
        else:
            rtext = util.discord.format("{!M}({!i})", id, id)
        output.append("role {}".format(rtext))
    await msg.channel.send(util.discord.format("Priv {!i} includes: {}", priv, "; ".join(output)),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_add_user(msg: discord.Message, priv: str, user_id: int) -> None:
    users = conf[priv, "users"] or FrozenList()
    if user_id in users:
        await msg.channel.send(util.discord.format("User {!m} is already in priv {!i}", user_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    conf[priv, "users"] = users + [user_id]
    await conf

    await msg.channel.send(util.discord.format("Added user {!m} to priv {!i}", user_id, priv),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_add_role(msg: discord.Message, priv: str, role_id: int) -> None:
    roles = conf[priv, "roles"] or FrozenList()
    if role_id in roles:
        await msg.channel.send(util.discord.format("Role {!M} is already in priv {!i}", role_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    conf[priv, "roles"] = roles + [role_id]
    await conf

    await msg.channel.send(util.discord.format("Added role {!M} to priv {!i}", role_id, priv),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_remove_user(msg: discord.Message, priv: str, user_id: int) -> None:
    users = conf[priv, "users"] or FrozenList()
    if user_id not in users:
        await msg.channel.send(util.discord.format("User {!m} is already not in priv {!i}",
            user_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    musers = users.copy()
    musers.remove(user_id)
    conf[priv, "users"] = musers
    await conf

    await msg.channel.send(util.discord.format("Removed user {!m} from priv {!i}", user_id, priv),
        allowed_mentions=discord.AllowedMentions.none())

async def priv_remove_role(msg: discord.Message, priv: str, role_id: int) -> None:
    roles = conf[priv, "roles"] or FrozenList()
    if role_id not in roles:
        await msg.channel.send(util.discord.format("Role {!M} is already not in priv {!i}", role_id, priv),
            allowed_mentions=discord.AllowedMentions.none())
        return

    mroles = roles.copy()
    mroles.remove(role_id)
    conf[priv, "roles"] = mroles
    await conf

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
        if conf[priv.text, "users"] is None and conf[priv.text, "roles"] is None:
            await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "user":
            arg = args.next_arg()
            if arg is None: return
            user_id = user_id_from_arg(msg.guild, arg)
            if user_id is None: return
            await priv_add_user(msg, priv.text, user_id)

        elif cmd.text.lower() == "role":
            arg = args.next_arg()
            if arg is None: return
            role_id = role_id_from_arg(msg.guild, arg)
            if role_id is None: return
            await priv_add_role(msg, priv.text, role_id)

    elif cmd.text.lower() == "remove":
        priv = args.next_arg()
        if not isinstance(priv, plugins.commands.StringArg): return
        if conf[priv.text, "users"] is None and conf[priv.text, "roles"] is None:
            await msg.channel.send(util.discord.format("Priv {!i} does not exist", priv.text))
            return
        cmd = args.next_arg()
        if not isinstance(cmd, plugins.commands.StringArg): return
        if cmd.text.lower() == "user":
            arg = args.next_arg()
            if arg is None: return
            user_id = user_id_from_arg(msg.guild, arg)
            if user_id is None: return
            await priv_remove_user(msg, priv.text, user_id)

        elif cmd.text.lower() == "role":
            arg = args.next_arg()
            if arg is None: return
            role_id = role_id_from_arg(msg.guild, arg)
            if role_id is None: return
            await priv_remove_role(msg, priv.text, role_id)
