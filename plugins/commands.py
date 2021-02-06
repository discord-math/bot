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

class TagArg:
    __slots__ = "id"
    def __init__(self, id):
        self.id = id

class UserMentionArg(TagArg):
    __slots__ = "has_nick"
    def __init__(self, id, has_nick):
        self.id = id
        self.has_nick = has_nick

class RoleMentionArg(TagArg):
    pass

class ChannelArg(TagArg):
    pass

class EmojiArg(TagArg):
    __slots__ = "name", "animated"
    def __init__(self, id, name, animated):
        self.id = id
        self.name = name
        self.animated = animated

class BracketedArg:
    __slots__ = "contents"
    def __init__(self, contents):
        self.contents = contents

    def __str__(self):
        return self.contents

class InlineCodeArg(BracketedArg):
    pass

class CodeBlockArg(BracketedArg):
    __slots__ = "language"
    def __init__(self, contents, language):
        self.contents = contents
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
        (?: # tag:
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

    def get_arg(self, emoji=False):
        regex = self.parse_re_emoji if emoji else self.parse_re_no_emoji
        match = regex.match(self.cmdline, self.pos)
        if not match:
            return None
        self.pos = match.end()
        if emoji and match["emoji"] != None:
            return EmojiArg(match["id"], match["emoji"],
                match["animated"] != None)
        elif match["type"] != None:
            type = match["type"]
            id = int(match["id"])
            if type == "@":
                return UserMentionArg(id, False)
            elif type == "@!":
                return UserMentionArg(id, True)
            elif type == "@&":
                return RoleMentionArg(id)
            elif type == "#":
                return ChannelArg(id)
        elif match["string"] != None:
            return re.sub(r"\\(.)", match["text"], r"\1")
        elif match["block"] != None:
            return CodeBlockArg(match["block"], match["language"])
        elif match["code1"] != None:
            return InlineCodeArg(match["code1"])
        elif match["code2"] != None:
            return InlineCodeArg(match["code2"])
        elif match["text"] != None:
            return match["text"]

    def get_string_arg(self, emoji=False):
        regex = self.parse_re_emoji if emoji else self.parse_re_no_emoji
        match = regex.match(self.cmdline, self.pos)
        if not match:
            return None
        self.pos = match.end()
        if match["tag"] != None:
            return match["tag"]
        elif match["string"] != None:
            return re.sub(r"\\(.)", match["text"], r"\1")
        elif match["block"] != None:
            return match["block"]
        elif match["code1"] != None:
            return match["code1"]
        elif match["code2"] != None:
            return match["code2"]
        elif match["text"] != None:
            return match["text"]

    def get_rest(self):
        return self.cmdline[self.pos:].lstrip()

commands = {}

@util.discord.event("message")
async def message_find_command(msg):
    if conf.prefix and msg.content.startswith(conf.prefix):
        parser = ArgParser(msg.content[len(conf.prefix):])
        name = parser.get_arg()
        if isinstance(name, str):
            name = name.lower()
            if name in commands:
                try:
                    await commands[name](msg, parser)
                except:
                    logger.error(
                        "Error in command {} in <#{}> from <#{}>".format(
                            name, msg.channel.id, msg.author.id
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
