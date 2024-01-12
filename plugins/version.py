import asyncio.subprocess

from discord.ext.commands import command

from bot.acl import privileged
from bot.commands import Context, cleanup, plugin_command
from util.discord import format


@plugin_command
@cleanup
@command("version")
@privileged
async def version_command(ctx: Context) -> None:
    """Display running bot version including any local changes."""
    git_log = await asyncio.subprocess.create_subprocess_exec(
        "git", "log", "--max-count=1", "--format=format:%H%d", "HEAD", stdout=asyncio.subprocess.PIPE
    )
    try:
        assert git_log.stdout
        version = (await git_log.stdout.read()).decode("utf", "replace").rstrip("\n")
    finally:
        await git_log.wait()

    git_status = await asyncio.subprocess.create_subprocess_exec(
        "git", "status", "--porcelain", "-z", stdout=asyncio.subprocess.PIPE
    )
    try:
        assert git_status.stdout
        changes = (await git_status.stdout.read()).decode("utf", "replace").split("\0")
    finally:
        await git_status.wait()

    changes = list(filter(lambda line: line and not line.startswith("??"), changes))

    if changes:
        await ctx.send("{} with changes:\n{}".format(version, "\n".join(format("{!i}", change) for change in changes)))
    else:
        await ctx.send(version)
