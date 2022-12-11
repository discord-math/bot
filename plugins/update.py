import asyncio
import asyncio.subprocess
from typing import Optional, Protocol, cast

from bot.commands import Context, cleanup, command
from bot.privileges import priv
import plugins
import util.db.kv
from util.discord import CodeItem, Typing, chunk_messages

class UpdateConf(Protocol):
    def __getitem__(self, key: str) -> Optional[str]: ...

conf: UpdateConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(UpdateConf, await util.db.kv.load(__name__))

@cleanup
@command("update")
@priv("admin")
async def update_command(ctx: Context, bot_directory: Optional[str]) -> None:
    """Pull changes from git remote."""
    cwd = conf[bot_directory] if bot_directory is not None else None
    git_pull = await asyncio.create_subprocess_exec("git", "pull", "--ff-only", "--recurse-submodules", cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    async with Typing(ctx):
        try:
            assert git_pull.stdout
            output = (await git_pull.stdout.read()).decode("utf", "replace")
        finally:
            await git_pull.wait()

    for content, files in chunk_messages((CodeItem(output, filename="update.txt"),)):
        await ctx.send(content, files=files)
