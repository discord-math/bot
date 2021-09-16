import collections
import discord
import discord.ext.commands
import discord.ext.typed_commands
from typing import List, Mapping, Optional, Set
import discord_client
import util.discord
import plugins
import plugins.commands
import plugins.privileges

class HelpCommand(discord.ext.typed_commands.HelpCommand[discord.ext.commands.Context]):
    async def send_bot_help(self,
        mapping: Mapping[Optional[discord.ext.typed_commands.Cog[discord.ext.commands.Context]],
        List[discord.ext.typed_commands.Command[discord.ext.commands.Context]]]) -> None:
        if self.context is None: return

        commands: Mapping[str, Set[discord.ext.typed_commands.Command[discord.ext.commands.Context]]]
        commands = collections.defaultdict(set)
        for cmds in mapping.values():
            for cmd in cmds:
                allowed = True
                for check in cmd.checks:
                    if isinstance(check, plugins.privileges.PrivCheck):
                        if not check(self.context):
                            allowed = False
                            break
                if allowed:
                    commands[cmd.module].add(cmd) # type: ignore

        listing = "\n".join("{}: {}".format(module.rsplit(".", 1)[-1],
                ", ".join(util.discord.format("{!i}", self.context.prefix + cmd.name)
                    for cmd in sorted(cmds, key=lambda c: c.name)))
            for module, cmds in sorted(commands.items(), key=lambda mc: mc[0].rsplit(".", 1)[-1]))

        await self.get_destination().send(
            util.discord.format("**Commands:**\n{}\n\nType {!i} for more info on a command.",
                listing, self.context.prefix + self.invoked_with + " <command name>"))

    async def send_command_help(self, command: discord.ext.typed_commands.Command[discord.ext.commands.Context]
        ) -> None:
        if self.context is None: return

        usage = self.context.prefix + " ".join(s for s in [command.qualified_name, command.signature] if s)
        akanote = "" if not command.aliases else "\naka: {}".format(", ".join(util.discord.format("{!i}", alias)
            for alias in command.aliases))
        desc = command.help

        allowed = True
        for check in command.checks:
            if isinstance(check, plugins.privileges.PrivCheck):
                if not check(self.context):
                    allowed = False
                    break
        privnote = "" if allowed else "\nYou are not allowed to use this command."

        await self.get_destination().send(util.discord.format("**Usage:** {!i}{}\n{}{}",
            usage, akanote, desc, privnote))

    async def send_group_help(self, group: discord.ext.typed_commands.Group[discord.ext.commands.Context]) -> None:
        if self.context is None: return

        args = [group.qualified_name, group.signature]
        if not group.invoke_without_command:
            args.append("...")

        usage = self.context.prefix + " ".join(s for s in args if s)
        akanote = "" if not group.aliases else "\naka: {}".format(", ".join(util.discord.format("{!i}", alias)
            for alias in group.aliases))
        desc = group.help

        subcommands = []
        for cmd in sorted(group.walk_commands(), key=lambda c: c.qualified_name):
            args = [cmd.name, cmd.signature]
            if isinstance(cmd, discord.ext.commands.Group) and not cmd.invoke_without_command:
                continue
            for parent in cmd.parents:
                if not isinstance(parent, discord.ext.commands.Group) or not parent.invoke_without_command:
                    args.insert(0, parent.signature)
                args.insert(0, parent.name)
            subcommands.append(util.discord.format("{!i}", self.context.prefix + " ".join(s for s in args if s)))

        allowed = True
        for check in group.checks:
            if isinstance(check, plugins.privileges.PrivCheck):
                if not check(self.context):
                    allowed = False
                    break
        privnote = "" if allowed else "\nYou are not allowed to use this command."

        await self.get_destination().send(util.discord.format(
            "**Usage:** {!i}{}\n{}\n**Sub-commands:**\n{}{}\n\nType {!i} for more info on a sub-command.",
            usage, akanote, desc, "\n".join(subcommands), privnote,
            self.context.prefix + self.invoked_with + " " + group.qualified_name + " <sub-command name>"))

old_help = discord_client.client.help_command
discord_client.client.help_command = HelpCommand()
@plugins.finalizer
def restore_help_command() -> None:
    discord_client.client.help_command = old_help
