import sys
import traceback
import importlib
import collections
import plugins
import plugins.autoload
import plugins.commands
import plugins.privileges
import util.discord
import util.restart

manager = plugins.PluginManager.of(__name__)
assert manager

@plugins.commands.command("restart")
@plugins.privileges.priv("admin")
async def restart_command(ctx: plugins.commands.Context) -> None:
    """Restart the bot process."""
    await ctx.send("Restarting...")
    util.restart.restart()

class PluginConverter(str):
    @classmethod
    async def convert(cls, ctx: plugins.commands.Context, arg: str) -> str:
        assert manager
        if not any(arg.startswith(namespace + ".") for namespace in manager.namespaces):
            arg = manager.namespaces[0] + "." + arg
        return arg

async def reply_exception(ctx: plugins.commands.Context) -> None:
    _, exc, tb = sys.exc_info()
    text = util.discord.format("{!b:py}", "".join(traceback.format_exception(None, exc, tb)))
    del tb
    await ctx.send(text)

@plugins.commands.cleanup
@plugins.commands.command("load")
@plugins.privileges.priv("admin")
async def load_command(ctx: plugins.commands.Context, plugin: PluginConverter) -> None:
    """Load a plugin."""
    try:
        await manager.load(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.cleanup
@plugins.commands.command("reload")
@plugins.privileges.priv("admin")
async def reload_command(ctx: plugins.commands.Context, plugin: PluginConverter) -> None:
    """Reload a plugin."""
    try:
        await manager.reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.cleanup
@plugins.commands.command("unsafereload")
@plugins.privileges.priv("admin")
async def unsafe_reload_command(ctx: plugins.commands.Context, plugin: PluginConverter) -> None:
    """Reload a plugin without its dependents."""
    try:
        await manager.unsafe_reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.cleanup
@plugins.commands.command("unload")
@plugins.privileges.priv("admin")
async def unload_command(ctx: plugins.commands.Context, plugin: PluginConverter) -> None:
    """Unload a plugin."""
    try:
        await manager.unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.cleanup
@plugins.commands.command("unsafeunload")
@plugins.privileges.priv("admin")
async def unsafe_unload_command(ctx: plugins.commands.Context, plugin: PluginConverter) -> None:
    """Unload a plugin without its dependents."""
    try:
        await manager.unsafe_unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.cleanup
@plugins.commands.command("reloadmod")
@plugins.privileges.priv("admin")
async def reloadmod_command(ctx: plugins.commands.Context, module: str) -> None:
    """Reload a module."""
    try:
        importlib.reload(sys.modules[module])
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.cleanup
@plugins.commands.group("autoload", invoke_without_command=True)
@plugins.privileges.priv("admin")
async def autoload_command(ctx: plugins.commands.Context) -> None:
    """Manage plugins loaded at startup."""
    await ctx.send(", ".join(util.discord.format("{!i}", name) for name in plugins.autoload.get_autoload()))

@autoload_command.command("add")
@plugins.privileges.priv("admin")
async def autoload_add(ctx: plugins.commands.Context, plugin: PluginConverter) -> None:
    """Add a plugin to be loaded at startup."""
    await plugins.autoload.set_autoload(plugin, True)
    await ctx.send("\u2705")

@autoload_command.command("remove")
@plugins.privileges.priv("admin")
async def autoload_remove(ctx: plugins.commands.Context, plugin: PluginConverter) -> None:
    """Remove a plugin from startup loading list."""
    await plugins.autoload.set_autoload(plugin, False)
    await ctx.send("\u2705")

@plugins.commands.cleanup
@plugins.commands.command("plugins")
@plugins.privileges.priv("mod")
async def plugins_command(ctx: plugins.commands.Context) -> None:
    """List loaded plugins."""
    output = collections.defaultdict(list)
    for name in sys.modules:
        if manager.is_plugin(name):
            try:
                key = manager.plugins[name].state.name
            except KeyError:
                key = "???"
            output[key].append(name)
    await ctx.send("\n".join(
        util.discord.format("{!i}: {}", key, ", ".join(util.discord.format("{!i}", name) for name in plugins))
        for key, plugins in output.items()))
