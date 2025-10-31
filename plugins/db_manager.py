from collections import defaultdict
from typing import Dict, List, Optional, Set, Union, cast

import asyncpg
import discord
from discord.ext.commands import Greedy, command, group
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import yaml

import bot.acl
from bot.acl import (
    ACL,
    ACLCheck,
    ActionPermissions,
    CommandPermissions,
    EvalResult,
    MessageableChannel,
    evaluate_acl,
    evaluate_acl_meta,
    live_actions,
    privileged,
    register_action,
)
import bot.autoload
from bot.client import client
import bot.commands
from bot.commands import Context, cleanup, plugin_command
from bot.config import plugin_config_command
from bot.reactions import get_reaction
from plugins.bot_manager import PluginConverter
import util.db
from util.discord import CodeBlock, Inline, PlainItem, Typing, UserError, chunk_messages, format


@plugin_command
@cleanup
@command("sql")
@privileged
async def sql_command(ctx: Context, args: Greedy[Union[CodeBlock, Inline, str]]) -> None:
    """Execute arbitrary SQL statements in the database."""
    data_outputs: List[List[str]] = []
    outputs: List[Union[str, List[str]]] = []
    async with util.db.connection() as conn:
        async with Typing(ctx):
            tx = conn.transaction()
            await tx.start()
            for arg in args:
                if isinstance(arg, (CodeBlock, Inline)):
                    try:
                        stmt = await conn.prepare(arg.text)
                        results = (await stmt.fetch())[:1000]
                    except asyncpg.PostgresError as e:
                        outputs.append(format("{!b}", e))
                    else:
                        outputs.append(stmt.get_statusmsg())
                        if results:
                            data = [" ".join(results[0].keys())]
                            data.extend(" ".join(repr(col) for col in result) for result in results)
                            if len(results) == 1000:
                                data.append("...")
                            data_outputs.append(data)
                            outputs.append(data)

        def output_len(output: List[str]) -> int:
            return sum(len(row) + 1 for row in output)

        total_len = sum(
            4 + output_len(output) + 4 if isinstance(output, list) else len(output) + 1 for output in outputs
        )

        while total_len > 2000 and any(data_outputs):
            lst = max(data_outputs, key=output_len)
            if lst[-1] == "...":
                removed = lst.pop(-2)
            else:
                removed = lst.pop()
                lst.append("...")
                total_len += 4
            total_len -= len(removed) + 1

        text = "\n".join(
            format("{!b}", "\n".join(output)) if isinstance(output, list) else output for output in outputs
        )[:2000]

        reply = await ctx.send(text)

        # If we've been assigned a transaction ID, means we've changed
        # something. Prompt the user to commit.
        has_tx = False
        try:
            if await conn.fetchval("SELECT txid_current_if_assigned()"):
                has_tx = True
        except asyncpg.PostgresError:
            pass
        if not has_tx:
            return

        if await get_reaction(reply, ctx.author, {"\u21A9": False, "\u2705": True}, timeout=60):
            await tx.commit()
        else:
            await tx.rollback()


@plugin_config_command
@command("prefix")
@privileged
async def config_prefix(ctx: Context, prefix: Optional[str]) -> None:
    async with AsyncSession(util.db.engine) as session:
        conf = await session.get(bot.commands.GlobalConfig, 0)
        assert conf
        if prefix is None:
            await ctx.send(format("{!i}", conf.prefix))
        else:
            conf.prefix = prefix
            await session.commit()
            await ctx.send("\u2705")


@plugin_command
@cleanup
@group("acl")
@privileged
async def acl_command(ctx: Context) -> None:
    """Manage Access Control Lists, their mapping to commands and actions."""


@acl_command.command("list")
@privileged
async def acl_list(ctx: Context) -> None:
    """List ACLs."""
    acls: Set[str] = set()
    used: Set[Optional[str]] = set()
    async with AsyncSession(util.db.engine) as session:
        stmt = select(bot.acl.ACL)
        for acl in (await session.execute(stmt)).scalars():
            acls.add(acl.name)
            used.add(acl.meta)
        stmt = select(bot.acl.CommandPermissions.acl)
        for acl in (await session.execute(stmt)).scalars():
            used.add(acl)
        stmt = select(bot.acl.ActionPermissions.acl)
        for acl in (await session.execute(stmt)).scalars():
            used.add(acl)
    output = "ACLs: {}".format(", ".join(format("{!i}", name) for name in acls if name in used))
    if len(acls - used):
        output += "\nUnused: {}".format(", ".join(format("{!i}", name) for name in acls if name not in used))
    await ctx.send(output)


@acl_command.command("show")
@privileged
async def acl_show(ctx: Context, *args: str) -> None:
    """
    Show the formula for the given ACL. Use --pretty or -p for Discord Markdown formatting with mention tags.
    """
    pretty = any(arg in ("--pretty", "-p") for arg in args)
    try:
        acl_name = next(arg for arg in args if not arg.startswith("-"))
    except StopIteration:
        raise UserError("Usage: `.acl show [--pretty|-p] <acl_name>`")

    async with AsyncSession(util.db.engine) as session:
        if (acl_obj := await session.get(bot.acl.ACL, acl_name)) is None:
            raise UserError(format("No such ACL: {!i}", acl_name))

    if pretty:
        await ctx.send(ACL.format_markdown(acl_obj.data), allowed_mentions=discord.AllowedMentions.none())
    else:
        await ctx.send(format("{!b:yaml}", yaml.dump(acl_obj.data)))


acl_override = register_action("acl_override")


@acl_command.command("set")
@privileged
async def acl_set(ctx: Context, acl: str, formula: CodeBlock) -> None:
    """Set the formula for the given ACL."""
    if evaluate_acl_meta(acl, ctx.author, cast(MessageableChannel, ctx.channel)) != EvalResult.TRUE:
        if (meta := bot.acl.acls[acl].meta) is None:
            raise UserError("You must match the `acl_override` action to edit this ACL")
        else:
            raise UserError(format("You do not match the meta-ACL {!i}", meta))

    try:
        data = yaml.safe_load(formula.text)
    except yaml.YAMLError as exc:
        raise UserError(str(exc))
    try:
        data = ACL.parse_data(data).serialize()
    except ValueError as exc:
        raise UserError(str(exc))

    async with AsyncSession(util.db.engine, expire_on_commit=False) as session:
        if obj := await session.get(bot.acl.ACL, acl):
            obj.data = data
        else:
            obj = bot.acl.ACL(name=acl, data=data)
            session.add(obj)
        await session.commit()
        bot.acl.acls[acl] = obj

    await ctx.send("\u2705")


@acl_command.command("commands")
@privileged
async def acl_commands(ctx: Context) -> None:
    """List commands that support ACLs, and the ACL's they're assigned to."""
    prefix: str = bot.commands.prefix
    acls: Dict[str, Set[str]] = defaultdict(set)
    seen: Set[str] = set()

    async with AsyncSession(util.db.engine) as session:
        stmt = select(bot.acl.CommandPermissions)
        for cmd in (await session.execute(stmt)).scalars():
            acls[cmd.acl].add(cmd.name)
            seen.add(cmd.name)

    output = [
        PlainItem(
            format(
                "- {} require the {!i} ACL\n",
                ", ".join(format("{!i}", prefix + command) for command in sorted(commands)),
                acl,
            )
        )
        for acl, commands in acls.items()
    ]

    used: Set[str] = set()
    for cmd in client.walk_commands():
        if any(isinstance(check, ACLCheck) for check in cmd.checks):
            used.add(cmd.qualified_name)
    if len(used - seen):
        output.append(
            PlainItem("- Inaccessible: " + ", ".join(format("{!i}", prefix + command) for command in used - seen))
        )

    if not output:
        output.append(PlainItem("No commands found"))

    for content, files in chunk_messages(output):
        await ctx.send(content, files=files)


@acl_command.command("command")
@privileged
async def acl_command_cmd(ctx: Context, command: str, acl: Optional[str]) -> None:
    """Restrict the use of the given command to the given ACL."""
    channel = cast(MessageableChannel, ctx.channel)
    old_acl = bot.acl.commands.get(command)
    if evaluate_acl_meta(old_acl, ctx.author, channel) != EvalResult.TRUE:
        if (meta := bot.acl.acls[old_acl].meta if old_acl is not None else None) is None:
            msg = "You must match the `acl_override` action to edit this command"
        else:
            msg = format("You do not match the meta-ACL {!i} of the previous ACL {!i}", meta, old_acl)
        raise UserError(msg)

    if acl is not None and acl not in bot.acl.acls:
        raise UserError(format("No such ACL: {!i}", acl))

    if evaluate_acl_meta(acl, ctx.author, channel) != EvalResult.TRUE:
        if (meta := bot.acl.acls[acl].meta if acl is not None else None) is None:
            reason = "the new ACL has no meta and you do not match the `acl_override` action"
        else:
            reason = format("you do not match the meta-ACL {!i} of the new ACL", meta)
        prompt = await ctx.send(
            "\u26A0 You will not be able to edit this command anymore, as {}, continue?".format(reason)
        )
        if await get_reaction(prompt, ctx.author, {"\u274C": False, "\u2705": True}, timeout=60) != True:
            return

    async with AsyncSession(util.db.engine) as session:
        if obj := await session.get(CommandPermissions, command):
            if acl is not None:
                obj.acl = acl
            else:
                await session.delete(obj)
        elif acl is not None:
            session.add(bot.acl.CommandPermissions(name=command, acl=acl))
        await session.commit()
        if acl is not None:
            bot.acl.commands[command] = acl
        else:
            del bot.acl.commands[command]

    await ctx.send("\u2705")


@acl_command.command("actions")
@privileged
async def acl_actions(ctx: Context) -> None:
    """List actions that support ACLs, and the ACL's they're assigned to."""
    acls: Dict[str, Set[str]] = defaultdict(set)
    seen: Set[str] = set()

    async with AsyncSession(util.db.engine) as session:
        stmt = select(bot.acl.ActionPermissions)
        for action in (await session.execute(stmt)).scalars():
            acls[action.acl].add(action.name)
            seen.add(action.name)
    output = "\n".join(
        format("- {} require the {!i} ACL", ", ".join(format("{!i}", action) for action in actions), acl)
        for acl, actions in acls.items()
    )

    used = {action for action, uses in live_actions.items() if uses}
    if len(used - seen):
        output += "\nInaccessible: " + ", ".join(format("{!i}", action) for action in used - seen)

    await ctx.send(output or "No actions registered")


@acl_command.command("action")
@privileged
async def acl_action(ctx: Context, action: str, acl: Optional[str]) -> None:
    """Restrict the use of the given action to the given ACL."""
    channel = cast(MessageableChannel, ctx.channel)
    old_acl = bot.acl.actions.get(action)
    if evaluate_acl_meta(old_acl, ctx.author, channel) != EvalResult.TRUE:
        if (meta := bot.acl.acls[old_acl].meta if old_acl is not None else None) is None:
            msg = "You must match the `acl_override` action to edit this action"
        else:
            msg = format("You do not match the meta-ACL {!i} of the previous ACL {!i}", meta, old_acl)
        raise UserError(msg)

    if acl is not None and acl not in bot.acl.acls:
        raise UserError(format("No such ACL: {!i}", acl))

    if evaluate_acl_meta(acl, ctx.author, channel) != EvalResult.TRUE:
        if (meta := bot.acl.acls[acl].meta if acl is not None else None) is None:
            reason = "the new ACL has no meta and you do not match the `acl_override` action"
        else:
            reason = format("you do not match the meta-ACL {!i} of the new ACL", meta)
        prompt = await ctx.send(
            "\u26A0 You will not be able to edit this action anymore, as {}, continue?".format(reason)
        )
        if await get_reaction(prompt, ctx.author, {"\u274C": False, "\u2705": True}, timeout=60) != True:
            return

    async with AsyncSession(util.db.engine) as session:
        if obj := await session.get(ActionPermissions, action):
            if acl is not None:
                obj.acl = acl
            else:
                await session.delete(obj)
        elif acl is not None:
            session.add(bot.acl.ActionPermissions(name=action, acl=acl))
        await session.commit()
        if acl is not None:
            bot.acl.actions[action] = acl
        else:
            del bot.acl.actions[action]

    await ctx.send("\u2705")


@acl_command.command("metas")
@privileged
async def acl_metas(ctx: Context) -> None:
    """List meta-ACLs for ACLs."""
    acls: Dict[str, Set[str]] = defaultdict(set)
    async with AsyncSession(util.db.engine) as session:
        stmt = select(bot.acl.ACL)
        for acl in (await session.execute(stmt)).scalars():
            if acl.meta is not None:
                acls[acl.meta].add(acl.name)
    output = "\n".join(
        format("- {} require the {!i} ACL to be edited", ", ".join(format("{!i}", acl) for acl in acls), meta)
        for meta, acls in acls.items()
    )
    await ctx.send(output or "No meta-ACLs assigned")


@acl_command.command("meta")
@privileged
async def acl_meta(ctx: Context, acl: str, meta: Optional[str]) -> None:
    """Restrict editing of the given ACL (and its associated commands and actions) to the given meta-ACL."""
    channel = cast(MessageableChannel, ctx.channel)

    if acl not in bot.acl.acls:
        raise UserError(format("No such ACL: {!i}", acl))

    if evaluate_acl_meta(acl, ctx.author, channel) != EvalResult.TRUE:
        if (old_meta := bot.acl.acls[acl].meta) is None:
            msg = "You must match the `acl_override` action to edit this ACL"
        else:
            msg = format("You do not match the previous meta-ACL {!i}", old_meta)
        raise UserError(msg)

    if meta is not None and meta not in bot.acl.acls:
        raise UserError(format("No such ACL: {!i}", meta))

    if max(evaluate_acl(meta, ctx.author, channel), acl_override.evaluate(ctx.author, channel)) != EvalResult.TRUE:
        if meta is None:
            reason = "the meta is to be removed and you do not match the `acl_override` action"
        else:
            reason = format("you do not match the new meta-ACL {!i}", meta)
        prompt = await ctx.send("\u26A0 You will not be able to edit this ACL anymore, as {}, continue?".format(reason))
        if await get_reaction(prompt, ctx.author, {"\u274C": False, "\u2705": True}, timeout=60) != True:
            return

    async with AsyncSession(util.db.engine, expire_on_commit=False) as session:
        obj = await session.get(bot.acl.ACL, acl)
        assert obj
        obj.meta = meta
        await session.commit()
        bot.acl.acls[acl] = obj

    await ctx.send("\u2705")


@plugin_config_command
@group("autoload", invoke_without_command=True)
@privileged
async def config_autoload(ctx: Context) -> None:
    order = defaultdict(list)
    stmt = select(bot.autoload.AutoloadedPlugin).order_by(bot.autoload.AutoloadedPlugin.order)
    async with AsyncSession(util.db.engine) as session:
        for plugin in (await session.execute(stmt)).scalars():
            order[plugin.order].append(plugin.name)

    await ctx.send(
        "\n".join(
            "- {}: {}".format(i, ", ".join(format("{!i}", plugin) for plugin in plugins))
            for i, plugins in order.items()
        )
    )


@config_autoload.command("add")
@privileged
async def config_autoload_add(ctx: Context, plugin: PluginConverter, order: int) -> None:
    async with AsyncSession(util.db.engine) as session:
        session.add(bot.autoload.AutoloadedPlugin(name=plugin, order=order))
        await session.commit()
        await ctx.send("\u2705")


@config_autoload.command("remove")
@privileged
async def config_autoload_remove(ctx: Context, plugin: PluginConverter) -> None:
    async with AsyncSession(util.db.engine) as session:
        await session.delete(await session.get(bot.autoload.AutoloadedPlugin, plugin))
        await session.commit()
        await ctx.send("\u2705")
