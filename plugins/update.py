import asyncio.subprocess
import discord
from typing import Optional, Protocol, cast
import plugins
import plugins.commands
import plugins.privileges
import util.discord
import util.db.kv

class UpdateConf(Protocol):
    def __getitem__(self, key: str) -> Optional[str]: ...

conf: UpdateConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(UpdateConf, await util.db.kv.load(__name__))

@plugins.commands.command_ext("update")
@plugins.privileges.priv_ext("admin")
async def update_command(ctx: discord.ext.commands.Context, bot_directory: Optional[str]) -> None:
    """Pull changes from git remote"""
    cwd = conf[bot_directory] if bot_directory is not None else None
    git_pull = await asyncio.create_subprocess_exec("git", "pull", "--ff-only", cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        assert git_pull.stdout
        output = (await git_pull.stdout.read()).decode("utf", "replace")
    finally:
        await git_pull.wait()

    await ctx.send(util.discord.format("{!b}", output))
