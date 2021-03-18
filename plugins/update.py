import asyncio
import plugins.commands
import plugins.privileges
import util.discord
import util.db.kv

conf = util.db.kv.Config(__name__)

@plugins.commands.command("update")
@plugins.privileges.priv("admin")
async def update_command(msg, args):
    name = args.next_arg()
    name = name.text if isinstance(name, plugins.commands.StringArg) else None

    git_pull = await asyncio.create_subprocess_exec("git", "pull", "--ff-only",
        cwd=conf[name],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        output = (await git_pull.stdout.read()).decode("utf", "replace")
    finally:
        await git_pull.wait()

    await msg.channel.send(util.discord.format("{!b}", output))
