import io
import ast
import builtins
import inspect
import sys
import types
import traceback
import plugins.commands
import plugins.privileges
import discord_client

@plugins.commands.command("exec")
@plugins.commands.command("eval")
@plugins.privileges.priv("admin")
async def run_code(msg, args):
    """
    Execute every code block in the commandline as python code. The code can
    be an expression or a series of statements. The code has all loaded modules
    in scope, as well as "msg" and "client". The print function is redirected.
    The code also can use top-level "await".
    """
    outputs = []
    code_scope = dict(sys.modules)
    # Using real builtins to avoid dependency tracking
    code_scope.update(builtins.__dict__)
    code_scope["msg"] = msg
    code_scope["client"] = discord_client.client
    def mk_code_print(fp):
        def code_print(*args, sep=" ", end="\n", file=fp, flush=False):
            return print(*args, sep=sep, end=end, file=file, flush=flush)
        return code_print
    try:
        while arg := args.get_arg():
            if (isinstance(arg, plugins.commands.CodeBlockArg)
                or isinstance(arg, plugins.commands.InlineCodeArg)):
                fp = io.StringIO()
                outputs.append(fp)
                code_scope["print"] = mk_code_print(fp)
                try:
                    code = compile(arg.contents, "<msg {}>".format(msg.id),
                        "eval", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                except:
                    code = compile(arg.contents, "<msg {}>".format(msg.id),
                        "exec", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                fun = types.FunctionType(code, code_scope)
                ret = fun()
                if inspect.iscoroutine(ret):
                    ret = await ret
                if ret != None:
                    mk_code_print(fp)(repr(ret))
    except:
        _, exc, tb = sys.exc_info()
        mk_code_print(fp)("".join(traceback.format_tb(tb)))
        mk_code_print(fp)(repr(exc))
        del tb

    def format_block(fp):
        text = fp.getvalue().replace("``", "`\u200D`")
        if len(text):
            return "```\n" + text + "```"
        else:
            return "\u2705"

    if len(outputs):
        output = "".join(format_block(fp) for fp in outputs)
        await msg.channel.send(output)
