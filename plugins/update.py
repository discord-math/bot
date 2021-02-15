import asyncio
import plugins.commands
import plugins.privileges
import util.discord

@plugins.commands.command("update")
@plugins.privileges.priv("admin")
async def update_command(msg, args):
    git_pull = await asyncio.create_subprocess_exec("git", "pull", "--ff-only",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        output = (await git_pull.stdout.read()).decode("utf", "replace")
    finally:
        await git_pull.wait()

    await msg.channel.send(util.discord.format("{!b}", output))
