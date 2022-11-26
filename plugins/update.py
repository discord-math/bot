import asyncio
import asyncio.subprocess
from typing import Optional, Protocol, cast

import bot.commands
import bot.privileges
import plugins
import util.db.kv
import util.discord

class UpdateConf(Protocol):
    def __getitem__(self, key: str) -> Optional[str]: ...

conf: UpdateConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(UpdateConf, await util.db.kv.load(__name__))

@bot.commands.cleanup
@bot.commands.command("update")
@bot.privileges.priv("admin")
async def update_command(ctx: bot.commands.Context, bot_directory: Optional[str]) -> None:
    """Pull changes from git remote."""
    cwd = conf[bot_directory] if bot_directory is not None else None
    git_pull = await asyncio.create_subprocess_exec("git", "pull", "--ff-only", "--recurse-submodules", cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        assert git_pull.stdout
        output = (await git_pull.stdout.read()).decode("utf", "replace")
    finally:
        await git_pull.wait()

    await ctx.send(util.discord.format("{!b}", output))
