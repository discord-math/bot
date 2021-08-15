import io
import ast
import builtins
import inspect
import sys
import types
import traceback
import os
import itertools
import discord
from typing import Dict, List, Iterator, Union, Any, Callable, Iterable, TypeVar
import plugins.commands
import plugins.privileges
import util.discord
import discord_client

T = TypeVar("T")

@plugins.commands.command_ext("exec", aliases=["eval"])
@plugins.privileges.priv_ext("shell")
async def exec_command(ctx: discord.ext.commands.Context,
    args: discord.ext.commands.Greedy[Union[util.discord.CodeBlock, util.discord.Inline, str]]) -> None:
    """
    Execute all code blocks in the command line as python code.
    The code can be an expression on a series of statements. The code has all loaded modules in scope, as well as "ctx"
    and "client". The print function is redirected. The code can also use top-level "await".
    """
    outputs = []
    code_scope: Dict[str, Any] = dict(sys.modules)
    # Using real builtins to avoid dependency tracking
    code_scope["__builtins__"] = builtins
    code_scope.update(builtins.__dict__)
    code_scope["ctx"] = ctx
    code_scope["client"] = discord_client.client
    def mk_code_print(fp: io.StringIO) -> Callable[..., None]:
        def code_print( # type: ignore
            *args, sep=" ", end="\n", file=fp, flush=False):
            return print(*args, sep=sep, end=end, file=file, flush=flush)
        return code_print
    try:
        for arg in args:
            if isinstance(arg, (util.discord.CodeBlock, util.discord.Inline)):
                fp = io.StringIO()
                outputs.append(fp)
                code_scope["print"] = mk_code_print(fp)
                try:
                    code = compile(arg.text, "<msg {}>".format(ctx.message.id),
                        "eval", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                except:
                    code = compile(arg.text, "<msg {}>".format(ctx.message.id),
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

    def chunk(xs: Iterable[T], n: int) -> Iterator[List[T]]:
        acc = []
        for x in xs:
            acc.append(x)
            if len(acc) >= n:
                yield acc
                acc = []
        if len(acc):
            yield acc

    # greedily concatenate short strings into groups of at most a certain length
    def chunk_concat(xss: Iterable[str], n: int) -> Iterator[str]:
        acc = ""
        empty = True
        for xs in xss:
            empty = False
            if len(acc) + len(xs) > n:
                yield acc
                acc = ""
            acc += xs
        if not empty:
            yield acc

    def format_block(fp: io.StringIO) -> str:
        text = fp.getvalue()
        return util.discord.format("{!b:py}", text) if len(text) else "\u2705"

    def is_short(fp: io.StringIO) -> bool:
        return len(format_block(fp)) <= 2000

    def make_file_output(idx: int, fp: io.StringIO) -> discord.File:
        return discord.File(io.BytesIO(fp.getvalue().encode("utf8")), filename="output{:d}.txt".format(idx))

    message_outputs = chunk_concat(
        (format_block(m) for m in outputs if is_short(m)),
        2000)
    file_outputs = chunk(
        (make_file_output(idx, fp) for idx, fp in enumerate(outputs, start = 1)
            if not is_short(fp)),
        10)

    for text, file_output in itertools.zip_longest(message_outputs, file_outputs):
        await ctx.send(text, files=file_output)
