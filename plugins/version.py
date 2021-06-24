import asyncio.subprocess
import discord
import plugins.commands
import plugins.privileges
import util.discord

@plugins.commands.command("version")
@plugins.privileges.priv("mod")
async def version_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    git_log = await asyncio.subprocess.create_subprocess_exec(
        "git", "log", "--max-count=1", "--format=format:%H%d", "HEAD",
        stdout=asyncio.subprocess.PIPE)
    try:
        assert git_log.stdout
        version = (await git_log.stdout.read()).decode("utf", "replace").rstrip("\n")
    finally:
        await git_log.wait()

    git_status = await asyncio.subprocess.create_subprocess_exec(
        "git", "status", "--porcelain", "-z",
        stdout=asyncio.subprocess.PIPE)
    try:
        assert git_status.stdout
        changes = (await git_status.stdout.read()).decode("utf", "replace").split("\0")
    finally:
        await git_status.wait()

    changes = list(filter(lambda line: line and not line.startswith("??"), changes))

    if changes:
        await msg.channel.send("{} with changes:\n{}".format(version,
            "\n".join(util.discord.format("{!i}", change) for change in changes)))
    else:
        await msg.channel.send(version)
