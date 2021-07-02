import sys
import traceback
import importlib
import discord
from typing import Optional
import plugins
import plugins.autoload
import plugins.commands
import plugins.privileges
import util.discord
import util.restart

@plugins.commands.command("restart")
@plugins.privileges.priv("admin")
async def restart_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    await msg.channel.send("Restarting...")
    util.restart.restart()

def plugin_from_arg(name: Optional[plugins.commands.Arg]) -> Optional[str]:
    if not isinstance(name, plugins.commands.StringArg): return None
    pname = name.text
    if not pname.startswith(plugins.plugins_namespace + "."):
        pname = plugins.plugins_namespace + "." + pname
    return pname

async def reply_exception(msg: discord.Message) -> None:
    _, exc, tb = sys.exc_info()
    text = util.discord.format("{!b:py}", "{}\n{}".format("".join(traceback.format_tb(tb)), repr(exc)))
    del tb
    await msg.channel.send(text)

@plugins.commands.command("load")
@plugins.privileges.priv("admin")
async def load_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    name = plugin_from_arg(args.next_arg())
    if name is None: return
    try:
        plugins.load(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)


@plugins.commands.command("reload")
@plugins.privileges.priv("admin")
async def reload_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    name = plugin_from_arg(args.next_arg())
    if name is None: return
    try:
        plugins.reload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("unsafereload")
@plugins.privileges.priv("admin")
async def unsafe_reload_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    name = plugin_from_arg(args.next_arg())
    if name is None: return
    try:
        plugins.unsafe_reload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("unload")
@plugins.privileges.priv("admin")
async def unload_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    name = plugin_from_arg(args.next_arg())
    if name is None: return
    try:
        plugins.unload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("unsafeunload")
@plugins.privileges.priv("admin")
async def unsafe_unload_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    name = plugin_from_arg(args.next_arg())
    if name is None: return
    try:
        plugins.unsafe_unload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("reloadmod")
@plugins.privileges.priv("admin")
async def reloadmod_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    name = args.next_arg()
    if not isinstance(name, plugins.commands.StringArg): return None
    mname = name.text
    try:
        importlib.reload(sys.modules[mname])
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("autoload")
@plugins.privileges.priv("admin")
async def autoload_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    cmd = args.next_arg()
    if cmd is None:
        await msg.channel.send(", ".join(util.discord.format("{!i}", name) for name in plugins.autoload.get_autoload()))
        return
    if not isinstance(cmd, plugins.commands.StringArg): return None
    if cmd.text.lower() == "add":
        name = plugin_from_arg(args.next_arg())
        if name is None: return
        plugins.autoload.set_autoload(plugins.autoload.get_autoload() + [name])
        await msg.channel.send("\u2705")
    elif cmd.text.lower() == "remove":
        name = plugin_from_arg(args.next_arg())
        if name is None: return
        plugins.autoload.set_autoload(list(filter(lambda n: n != name, plugins.autoload.get_autoload())))
        await msg.channel.send("\u2705")

@plugins.commands.command("plugins")
@plugins.privileges.priv("mod")
async def plugins_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    await msg.channel.send(", ".join(util.discord.format("{!i}", name)
        for name in sys.modules if plugins.is_plugin(name)))
