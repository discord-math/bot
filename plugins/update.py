import asyncio.subprocess
import discord
import plugins.commands
import plugins.privileges
import util.discord
import util.db.kv

conf: util.db.kv.Config = util.db.kv.Config(__name__)

@plugins.commands.command("update")
@plugins.privileges.priv("admin")
async def update_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    cwd = None
    name = args.next_arg()
    if isinstance(name, plugins.commands.StringArg):
        cwd = conf[name.text]

    git_pull = await asyncio.create_subprocess_exec("git", "pull", "--ff-only", cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        assert git_pull.stdout
        output = (await git_pull.stdout.read()).decode("utf", "replace")
    finally:
        await git_pull.wait()

    await msg.channel.send(util.discord.format("{!b}", output))
