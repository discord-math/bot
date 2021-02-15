import jq
import plugins.commands
import plugins.privileges
import util.discord
import util.db.kv

@plugins.commands.command("config")
@plugins.privileges.priv("shell")
async def config_command(msg, args):
    arg = args.next_arg()
    if arg == None:
        return await msg.channel.send(", ".join(
            util.discord.format("{!i}", nsp)
            for nsp in util.db.kv.get_namespaces()))

    if not isinstance(arg, plugins.commands.StringArg): return

    if arg.text == "--delete":
        nsp = args.next_arg()
        key = args.next_arg()
        if not isinstance(nsp, plugins.commands.StringArg): return
        if not isinstance(key, plugins.commands.StringArg): return

        util.db.kv.Config(nsp.text)[key.text] = None
        return await msg.channel.send("\u2705")

    nsp = arg
    key = args.next_arg()
    if key == None:
        return await msg.channel.send(", ".join(
            util.discord.format("{!i}", key)
            for key in util.db.kv.Config(nsp.text)))

    if not isinstance(key, plugins.commands.StringArg): return

    script = args.next_arg()
    if script == None:
        result = util.db.kv.Config(nsp.text)._config.get(key.text)
        if result == None:
            return await msg.channel.send("None")
        else:
            return await msg.channel.send(util.discord.format("{!i}", result))

    conf = util.db.kv.Config(nsp.text)
    input = conf._config.get(key.text, "null")
    conf[key.text] = jq.compile(script.text).input(text=input).first()
    return await msg.channel.send("\u2705")
