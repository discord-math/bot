import sys
import traceback
import importlib
import plugins
import plugins.autoload
import plugins.commands
import plugins.privileges
import util.discord
import util.restart

@plugins.commands.command("restart")
@plugins.privileges.priv("admin")
async def restart_command(msg, args):
    util.restart.restart()
    await msg.channel.send("Restarting...")

def plugin_from_arg(name):
    if not isinstance(name, plugins.commands.StringArg): return None
    name = name.text
    if not name.startswith(plugins.plugins_namespace + "."):
        name = plugins.plugins_namespace + "." + name
    return name

async def reply_exception(msg):
    _, exc, tb = sys.exc_info()
    text = "{}".format(util.discord.CodeBlock(
        "{}\n{}".format("".join(traceback.format_tb(tb)), repr(exc))))
    del tb
    await msg.channel.send(text)

@plugins.commands.command("load")
@plugins.privileges.priv("admin")
async def load_command(msg, args):
    name = plugin_from_arg(args.next_arg())
    if not name: return
    try:
        plugins.load(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)


@plugins.commands.command("reload")
@plugins.privileges.priv("admin")
async def reload_command(msg, args):
    name = plugin_from_arg(args.next_arg())
    if not name: return
    try:
        plugins.reload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("unsafereload")
@plugins.privileges.priv("admin")
async def unsafe_reload_command(msg, args):
    name = plugin_from_arg(args.next_arg())
    if not name: return
    try:
        plugins.unsafe_reload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("unload")
@plugins.privileges.priv("admin")
async def unload_command(msg, args):
    name = plugin_from_arg(args.next_arg())
    if not name: return
    try:
        plugins.unload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("unsafeunload")
@plugins.privileges.priv("admin")
async def unsafe_unload_command(msg, args):
    name = plugin_from_arg(args.next_arg())
    if not name: return
    try:
        plugins.unsafe_unload(name)
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("reloadmod")
@plugins.privileges.priv("admin")
async def reloadmod_command(msg, args):
    name = args.next_arg()
    if not isinstance(name, plugins.commands.StringArg): return None
    name = name.text
    try:
        importlib.reload(sys.modules[name])
        await msg.channel.send("\u2705")
    except:
        await reply_exception(msg)

@plugins.commands.command("autoload")
@plugins.privileges.priv("admin")
async def autoload_command(msg, args):
    cmd = args.next_arg()
    if cmd == None:
        return await msg.channel.send(", ".join(
            "{}".format(util.discord.Inline(name))
            for name in plugins.autoload.get_autoload()))
    if not isinstance(cmd, plugins.commands.StringArg): return None
    if cmd.text.lower() == "add":
        name = plugin_from_arg(args.next_arg())
        if not name: return
        plugins.autoload.set_autoload(plugins.autoload.get_autoload() + [name])
        await msg.channel.send("\u2705")
    elif cmd.text.lower() == "remove":
        name = plugin_from_arg(args.next_arg())
        if not name: return
        plugins.autoload.set_autoload(list(filter(lambda n: n != name,
            plugins.autoload.get_autoload())))
        await msg.channel.send("\u2705")
