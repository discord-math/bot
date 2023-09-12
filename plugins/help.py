from collections import defaultdict
from typing import Any, List, Mapping, Optional, Set

import discord.ext.commands
from discord.ext.commands import Cog, Command, Group

from bot.client import client
from bot.privileges import PrivCheck
import plugins
from util.discord import format

class HelpCommand(discord.ext.commands.HelpCommand):
    async def send_bot_help(self,
        mapping: Mapping[Optional[Cog], List[Command[Any, Any, Any]]]
        ) -> None:
        if self.context is None: return

        commands: Mapping[str, Set[Command[Any, Any, Any]]] = defaultdict(set)
        for cmds in mapping.values():
            for cmd in cmds:
                allowed = True
                for check in cmd.checks:
                    if isinstance(check, PrivCheck):
                        if not check(self.context): # type: ignore
                            allowed = False
                            break
                if allowed:
                    commands[cmd.module].add(cmd)
        prefix = self.context.prefix or ""

        listing = "\n".join("{}: {}".format(module.rsplit(".", 1)[-1],
                ", ".join(format("{!i}", prefix + cmd.name) for cmd in sorted(cmds, key=lambda c: c.name)))
            for module, cmds in sorted(commands.items(), key=lambda mc: mc[0].rsplit(".", 1)[-1]))

        await self.get_destination().send(
            format("**Commands:**\n{}\n\nType {!i} for more info on a command.",
                listing, prefix + (self.invoked_with or "") + " <command name>"))

    async def send_command_help(self, command: Command[Any, Any, Any]) -> None:
        if self.context is None: return

        prefix = self.context.prefix or ""
        usage = prefix + " ".join(s for s in [command.qualified_name, command.signature] if s)
        akanote = "" if not command.aliases else "\naka: {}".format(", ".join(format("{!i}", alias)
            for alias in command.aliases))
        desc = command.help

        allowed = True
        for check in command.checks:
            if isinstance(check, PrivCheck):
                if not check(self.context): # type: ignore
                    allowed = False
                    break
        privnote = "" if allowed else "\nYou are not allowed to use this command."

        await self.get_destination().send(format("**Usage:** {!i}{}\n{}{}",
            usage, akanote, desc, privnote))

    async def send_group_help(self, group: Group[Any, Any, Any]) -> None:
        if self.context is None: return

        prefix = self.context.prefix or ""
        args = [group.qualified_name, group.signature]
        if not group.invoke_without_command:
            args.append("...")

        usage = prefix + " ".join(s for s in args if s)
        akanote = "" if not group.aliases else "\naka: {}".format(", ".join(format("{!i}", alias)
            for alias in group.aliases))
        desc = group.help

        subcommands = []
        for cmd in sorted(group.walk_commands(), key=lambda c: c.qualified_name):
            args = [cmd.name, cmd.signature]
            if isinstance(cmd, Group) and not cmd.invoke_without_command:
                continue
            for parent in cmd.parents:
                if not parent.invoke_without_command:
                    args.insert(0, parent.signature)
                args.insert(0, parent.name)
            subcommands.append(format("{!i}", prefix + " ".join(s for s in args if s)))

        allowed = True
        for check in group.checks:
            if isinstance(check, PrivCheck):
                if not check(self.context): # type: ignore
                    allowed = False
                    break
        privnote = "" if allowed else "\nYou are not allowed to use this command."

        await self.get_destination().send(format(
            "**Usage:** {!i}{}\n{}\n**Sub-commands:**\n{}{}\n\nType {!i} for more info on a sub-command.",
            usage, akanote, desc, "\n".join(subcommands), privnote,
            prefix + (self.invoked_with or "") + " " + group.qualified_name + " <sub-command name>"))

old_help = client.help_command
client.help_command = HelpCommand()
@plugins.finalizer
def restore_help_command() -> None:
    client.help_command = old_help
