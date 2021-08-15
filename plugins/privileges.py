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

class PrivContext(discord.ext.commands.Context):
    priv: str

@plugins.commands.command_ext("priv", cls=discord.ext.commands.Group)
@priv_ext("shell")
async def priv_command(ctx: discord.ext.commands.Context) -> None:
    """Manage privilege sets"""
    pass

def priv_exists(priv: str) -> bool:
    return conf[priv, "users"] is not None or conf[priv, "roles"] is not None

def validate_priv(priv: str) -> None:
    if not priv_exists(priv):
        raise util.discord.UserError(util.discord.format("Priv {!i} does not exist", priv))

@priv_command.command("new")
async def priv_new(ctx: discord.ext.commands.Context, priv: str) -> None:
    """Create a new priv"""
    if priv_exists(priv):
        raise util.discord.UserError(util.discord.format("Priv {!i} already exists", priv))

    conf[priv, "users"] = []
    conf[priv, "roles"] = []
    await conf

    await ctx.send(util.discord.format("Created priv {!i}", priv))

@priv_command.command("delete")
async def priv_delete(ctx: discord.ext.commands.Context, priv: str) -> None:
    """Delete a priv"""
    validate_priv(priv)

    conf[priv, "users"] = None
    conf[priv, "roles"] = None
    await conf

    await ctx.send(util.discord.format("Removed priv {!i}", priv))

@priv_command.command("show")
async def priv_show(ctx: discord.ext.commands.Context, priv: str) -> None:
    """Show the users and roles in a priv"""
    validate_priv(priv)
    users = conf[priv, "users"]
    roles = conf[priv, "roles"]
    output = []
    for id in users or ():
        user = await discord_client.client.fetch_user(id)
        if user is not None:
            mtext = util.discord.format("{!m}({!i} {!i})", user, user.name, user.id)
        else:
            mtext = util.discord.format("{!m}({!i})", id, id)
        output.append("user {}".format(mtext))
    for id in roles or ():
        role = discord.utils.find(lambda r: r.id == id, ctx.guild.roles if ctx.guild is not None else ())
        if role is not None:
            rtext = util.discord.format("{!M}({!i} {!i})", role, role.name, role.id)
        else:
            rtext = util.discord.format("{!M}({!i})", id, id)
        output.append("role {}".format(rtext))
    await ctx.send(util.discord.format("Priv {!i} includes: {}", priv, "; ".join(output)),
        allowed_mentions=discord.AllowedMentions.none())

@priv_command.group("add")
async def priv_add(ctx: PrivContext, priv: str) -> None:
    """Add a user or role to a priv"""
    validate_priv(priv)
    ctx.priv = priv

@priv_add.command("user")
async def priv_add_user(ctx: PrivContext, user: util.discord.PartialUserConverter) -> None:
    """Add a user to a priv"""
    priv = ctx.priv
    users = conf[priv, "users"] or FrozenList()
    if user.id in users:
        raise util.discord.UserError(util.discord.format("User {!m} is already in priv {!i}", user.id, priv))

    conf[priv, "users"] = users + [user.id]
    await conf

    await ctx.send(util.discord.format("Added user {!m} to priv {!i}", user.id, priv),
        allowed_mentions=discord.AllowedMentions.none())

@priv_add.command("role")
async def priv_add_role(ctx: PrivContext, role: util.discord.PartialRoleConverter) -> None:
    """Add a role to a priv"""
    priv = ctx.priv
    roles = conf[priv, "roles"] or FrozenList()
    if role.id in roles:
        raise util.discord.UserError(util.discord.format("Role {!M} is already in priv {!i}", role.id, priv))

    conf[priv, "roles"] = roles + [role.id]
    await conf

    await ctx.send(util.discord.format("Added role {!M} to priv {!i}", role.id, priv),
        allowed_mentions=discord.AllowedMentions.none())

@priv_command.group("remove")
async def priv_remove(ctx: PrivContext, priv: str) -> None:
    """Remove a user or role from a priv"""
    validate_priv(priv)
    ctx.priv = priv

@priv_remove.command("user")
async def priv_remove_user(ctx: PrivContext, user: util.discord.PartialUserConverter) -> None:
    """Remove a user from a priv"""
    priv = ctx.priv
    users = conf[priv, "users"] or FrozenList()
    if user.id not in users:
        raise util.discord.UserError(util.discord.format("User {!m} is already not in priv {!i}", user.id, priv))

    musers = users.copy()
    musers.remove(user.id)
    conf[priv, "users"] = musers

    await ctx.send(util.discord.format("Removed user {!m} from priv {!i}", user.id, priv),
        allowed_mentions=discord.AllowedMentions.none())

@priv_remove.command("role")
async def priv_remove_role(ctx: PrivContext, role: util.discord.PartialRoleConverter) -> None:
    """Remove a role from a priv"""
    priv = ctx.priv
    roles = conf[priv, "roles"] or FrozenList()
    if role.id not in roles:
        raise util.discord.UserError(util.discord.format("Role {!M} is already not in priv {!i}", role.id, priv))

    mroles = roles.copy()
    mroles.remove(role.id)
    conf[priv, "roles"] = mroles

    await ctx.send(util.discord.format("Removed role {!M} from priv {!i}", role.id, priv),
        allowed_mentions=discord.AllowedMentions.none())
