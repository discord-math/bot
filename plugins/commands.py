"""
Utilities for registering basic commands. Commands are triggered by a
configurable prefix.
"""

import re
import asyncio
import logging
import util.discord
import util.db.kv
import plugins

logger = logging.getLogger(__name__)
conf = util.db.kv.Config(__name__)

class Arg:
    __slots__ = "source", "rest"
    def __init__(self, source, rest):
        self.source = source
        self.rest = rest

class TagArg(Arg):
    __slots__ = "id"
    def __init__(self, source, rest, id):
        super().__init__(source, rest)
        self.id = id

class UserMentionArg(TagArg):
    __slots__ = "has_nick"
    def __init__(self, source, rest, id, has_nick):
        super().__init__(source, rest, id)
        self.has_nick = has_nick

class RoleMentionArg(TagArg):
    __slots__ = ()

class ChannelArg(TagArg):
    __slots__ = ()

class EmojiArg(TagArg):
    __slots__ = "name", "animated"
    def __init__(self, source, rest, id, name, animated):
        super().__init__(source, rest, id)
        self.name = name
        self.animated = animated

class StringArg(Arg):
    __slots__ = "text"
    def __init__(self, source, rest, text):
        super().__init__(source, rest)
        self.text = text

class InlineCodeArg(StringArg):
    __slots__ = ()

class CodeBlockArg(StringArg):
    __slots__ = "language"
    def __init__(self, source, rest, text, language):
        super().__init__(source, rest, text)
        self.language = language

class ArgParser:
    """
    Parse a commandline into a sequence of words, quoted strings, code blocks,
    user or role mentions, channel links, and optionally emojis.
    """

    __slots__ = "cmdline", "pos"

    def __init__(self, text):
        self.cmdline = text.lstrip()
        self.pos = 0

    parse_re = r"""
        \s*
        (?P<source> # tag:
            (?P<tag><
                (?P<type>@!?|@&|[#]{})
                (?P<id>\d+)
            >)
        |   # quoted string:
            "
                (?P<string>(?:\\.|[^"])*)
            "
        |   # code block:
            ```
                (?P<language>\S*\n(?!```))?
                (?P<block>(?:(?!```).)+)
            ```
        |   # inline code:
            ``
                (?P<code2>(?:(?!``).)+)
            ``
        |   # inline code:
            `
                (?P<code1>[^`]+)
            `
        |   # regular word, up to first tag:
            (?P<text>(?:(?!<(?:@!?|@&|[#]{})\d+>)\S)+)
        )
        """
    parse_re_emoji = re.compile(
        parse_re.format(r"|(?P<animated>a)?:(?P<emoji>\w*):", r"|a?:\w*:"),
        re.S | re.X)
    parse_re_no_emoji = re.compile(parse_re.format(r"", r""), re.S | re.X)

    def __iter__(self):
        while (arg := self.next_arg()) != None:
            yield arg

    def get_rest(self):
        return self.cmdline[self.pos:].lstrip()

    def next_arg(self, chunk_emoji=False):
        regex = self.parse_re_emoji if chunk_emoji else self.parse_re_no_emoji
        match = regex.match(self.cmdline, self.pos)
        if match == None: return None
        self.pos = match.end()
        if chunk_emoji and match["emoji"] != None:
            return EmojiArg(match["source"], self.get_rest(), match["id"],
                match["emoji"], match["animated"] != None)
        elif match["type"] != None:
            type = match["type"]
            id = int(match["id"])
            if type == "@":
                return UserMentionArg(match["source"], self.get_rest(),
                    id, False)
            elif type == "@!":
                return UserMentionArg(match["source"], self.get_rest(),
                    id, True)
            elif type == "@&":
                return RoleMentionArg(match["source"], self.get_rest(), id)
            elif type == "#":
                return ChannelArg(match["source"], self.get_rest(), id)
        elif match["string"] != None:
            return StringArg(match["source"], self.get_rest(),
                re.sub(r"\\(.)", r"\1", match["string"]))
        elif match["block"] != None:
            return CodeBlockArg(match["source"], self.get_rest(),
                match["block"], match["language"])
        elif match["code1"] != None:
            return InlineCodeArg(match["source"], self.get_rest(),
                match["code1"])
        elif match["code2"] != None:
            return InlineCodeArg(match["source"], self.get_rest(),
                match["code2"])
        elif match["text"] != None:
            return StringArg(match["source"], self.get_rest(), match["text"])
        return None

commands = {}

@util.discord.event("message")
async def message_find_command(msg):
    if conf.prefix and msg.content.startswith(conf.prefix):
        cmdline = msg.content[len(conf.prefix):]
        parser = ArgParser(cmdline)
        cmd = parser.next_arg()
        if isinstance(cmd, StringArg):
            name = cmd.text.lower()
            if name in commands:
                try:
                    logger.info("Command {!r} from <@{}> in <#{}>".format(
                        cmdline, msg.author.id, msg.channel.id))
                    await commands[name](msg, parser)
                except util.discord.UserError as exc:
                    await msg.channel.send("Error: {}".format(exc.text))
                except:
                    logger.error(
                        "Error in command {!r} from <@{}> in <#{}>".format(
                            name, msg.author.id, msg.channel.id
                        ), exc_info=True)

def unsafe_hook_command(name, fun):
    if not asyncio.iscoroutinefunction(fun):
        raise TypeError("expected coroutine function")
    if name in commands:
        raise ValueError("command {} already registered".format(name))
    commands[name] = fun

def unsafe_unhook_command(name, fun):
    if name not in commands:
        raise ValueError("command {} is not registered".format(name))
    del commands[name]

def command(name):
    """
    This decorator registers a function as a command with a given name. The
    function receives the Message and an ArgParser arguments. Only one function
    can be assigned to a given command name. This registers a finalizer that
    removes the command, so should only be called during plugin initialization.
    """
    def decorator(fun):
        fun.__name__ = name
        unsafe_hook_command(name, fun)
        @plugins.finalizer
        def finalizer():
            unsafe_unhook_command(name, fun)
        return fun
    return decorator
