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
import itertools 
import os

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
        chunk_pos = range(0, len(l), n)
        return [l[i:i+n] for i in chunk_pos]

    # greedily concatenate strings into groups of at most a certain length
    def chunk_concat(l, n): 
        chunks_concat = [""] if len(l) else [] 
        for text in l:
            new_len = len(chunks_concat[-1] + text)
            if new_len > n: 
                chunks_concat.append("") 
            chunks_concat[-1] = chunks_concat[-1] + text 
        return chunks_concat      

    def format_block(fp):
        text = fp.getvalue()
        return util.discord.format("{!b:py}", text) if len(text) else "\u2705"
    
    def short_heuristic(fp): 
        initial_pos = fp.tell() 
        fp.seek(0, os.SEEK_END) 
        fp_len = fp.tell() 
        fp.seek(initial_pos) 
        return fp_len <= 2000 and len(format_block(fp)) <= 2000
    
    def make_file_output(idx, fp): 
        fp.seek(0)
        discord_filename = "output{:d}.txt".format(idx)
        discord_file = discord.File(fp, filename = discord_filename)
        return discord_file

    message_outputs = [format_block(m) for m in outputs if short_heuristic(m)] 
    message_outputs_chunked = chunk_concat(message_outputs, 2000) 

    enumeration_readable = enumerate(outputs, start = 1) 
    file_outputs = [m for m in enumeration_readable if not short_heuristic(m[1])]
    file_outputs_chunked = chunk(file_outputs, 10) 

    output_messages = itertools.zip_longest(message_outputs_chunked, file_outputs_chunked)
    
    for text, file_info in output_messages:
        file_output = None
        if file_info: 
            file_output = [make_file_output(*args) for args in file_info]
        await msg.channel.send(text, files = file_output)

