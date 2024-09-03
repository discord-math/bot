import ast
import builtins
import inspect
from io import StringIO
import sys
import traceback
from types import FunctionType
from typing import Any, Callable, Dict, TypeVar, Union

from discord.ext.commands import Greedy, command

from bot.acl import privileged
from bot.client import client
from bot.commands import Context, cleanup, plugin_command
from util.discord import CodeBlock, CodeItem, Inline, PlainItem, Typing, chunk_messages


T = TypeVar("T")


@plugin_command
@cleanup
@command("exec", aliases=["eval"])
@privileged
async def exec_command(ctx: Context, args: Greedy[Union[CodeBlock, Inline, str]]) -> None:
    """
    Execute all code blocks in the command line as python code.
    The code can be an expression on a series of statements. The code has all loaded modules in scope, as well as "ctx"
    and "client". The print function is redirected. The code can also use top-level "await".
    """
    outputs = []
    code_scope: Dict[str, object] = dict(sys.modules)
    # Using real builtins to avoid dependency tracking
    code_scope["__builtins__"] = builtins
    code_scope.update(builtins.__dict__)
    code_scope["ctx"] = ctx
    code_scope["client"] = client

    def mk_code_print(fp: StringIO) -> Callable[..., None]:
        def code_print(*args: object, sep: str = " ", end: str = "\n", file: Any = fp, flush: bool = False):
            return print(*args, sep=sep, end=end, file=file, flush=flush)

        return code_print

    fp = StringIO()
    try:
        async with Typing(ctx):
            for arg in args:
                if isinstance(arg, (CodeBlock, Inline)):
                    fp = StringIO()
                    outputs.append(fp)
                    code_scope["print"] = mk_code_print(fp)
                    try:
                        code = compile(
                            arg.text, "<msg {}>".format(ctx.message.id), "eval", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
                        )
                    except:
                        code = compile(
                            arg.text, "<msg {}>".format(ctx.message.id), "exec", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
                        )
                    fun = FunctionType(code, code_scope)
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

    for content, files in chunk_messages(
        (
            CodeItem(fp.getvalue(), language="py", filename="output{}.txt".format(i))
            if fp.getvalue()
            else PlainItem("\u2705")
        )
        for i, fp in enumerate(outputs, start=1)
    ):
        await ctx.send(content, files=files)
