import io
import ast
import builtins
import inspect
import sys
import types
import traceback
import plugins.commands
import plugins.privileges
import discord
import util.discord
import discord_client
import os
from itertools import islice, zip_longest

@plugins.commands.command("exec")
@plugins.commands.command("eval")
@plugins.privileges.priv("shell")
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
    code_scope["__builtins__"] = builtins
    code_scope.update(builtins.__dict__)
    code_scope["msg"] = msg
    code_scope["client"] = discord_client.client
    def mk_code_print(fp):
        def code_print(*args, sep=" ", end="\n", file=fp, flush=False):
            return print(*args, sep=sep, end=end, file=file, flush=flush)
        return code_print
    try:
        for arg in args:
            if (isinstance(arg, plugins.commands.CodeBlockArg)
                or isinstance(arg, plugins.commands.InlineCodeArg)):
                fp = io.StringIO()
                outputs.append(fp)
                code_scope["print"] = mk_code_print(fp)
                try:
                    code = compile(arg.text, "<msg {}>".format(msg.id),
                        "eval", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                except:
                    code = compile(arg.text, "<msg {}>".format(msg.id),
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

    def chunk(l, n):
        value_iterator = iter(l) 
        return iter(lambda: list(islice(value_iterator, n)), [])

    # greedily concatenate short strings into groups of at most a certain length
    def chunk_concat(l, n): 
        current_value = "" 
        inhabited = False 
        for text in l:
            inhabited = True 
            if len(current_value + text) > n: 
                yield current_value 
                current_value = "" 
            current_value = current_value + text 
        if inhabited: 
            yield current_value 

    def format_block(fp):
        text = fp.getvalue()
        return util.discord.format("{!b:py}", text) if len(text) else "\u2705"
    
    def short_heuristic(fp): 
        return len(format_block(fp)) <= 2000
    
    def make_file_output(idx, fp): 
        fp.seek(0)
        return discord.File(fp, filename = "output{:d}.txt".format(idx))

    message_outputs = chunk_concat((format_block(m) for m in outputs if short_heuristic(m)), 2000) 
    file_outputs = chunk((make_file_output(*m) for m in enumerate(outputs, start = 1) if not short_heuristic(m[1])), 10)  
    
    for text, file_output in zip_longest(message_outputs, file_outputs):
        await msg.channel.send(text, files = file_output)

