import subprocess
import plugins.commands
import plugins.privileges
import util.discord

@plugins.commands.command("version")
@plugins.privileges.priv("admin")
async def version_command(msg, args):
    with subprocess.Popen(
        ["git", "log", "--max-count=1", "--format=format:%H%d", "HEAD"],
        stdout=subprocess.PIPE) as git_log:
        version = git_log.stdout.read().decode("utf", "replace").rstrip("\n")

    with subprocess.Popen(
        ["git", "status", "--porcelain", "-z"],
        stdout=subprocess.PIPE) as git_status:
        changes = git_status.stdout.read().decode("utf", "replace").split("\0")
    changes = list(filter(lambda line: line and not line.startswith("??"),
        changes))

    if changes:
        await msg.channel.send("{} with changes:\n{}".format(version,
            "\n".join(util.discord.format("{!i}", change)
                for change in changes)))
    else:
        await msg.channel.send(version)
