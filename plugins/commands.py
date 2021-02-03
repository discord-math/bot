import re
import asyncio
import logging
import util.discord
import util.db.kv
import plugins

logger = logging.getLogger(__name__)
conf = util.db.kv.Config(__name__)

class TagArg:
    def __init__(self, id):
        self.id = id

class UserMentionArg(TagArg):
    def __init__(self, id, has_nick):
        self.id = id
        self.has_nick = has_nick

class RoleMentionArg(TagArg):
    pass

class ChannelArg(TagArg):
    pass

class EmojiArg(TagArg):
    def __init__(self, id, name, animated):
        self.id = id
        self.name = name
        self.animated = animated

class BracketedArg:
    def __init__(self, contents):
        self.contents = contents

    def __str__(self):
        return self.contents

class InlineCodeArg(BracketedArg):
    pass

class CodeBlockArg(BracketedArg):
    def __init__(self, contents, language):
        self.contents = contents
        self.language = language

class SpoilerArg(BracketedArg):
    pass

class ArgParser:
    def __init__(self, text):
        self.cmdline = text.lstrip()
        self.pos = 0

    parse_re = r"""
        \s*
        (?: # tag:
            <
                (?P<type>@!?|@&|[#]{})
                (?P<id>\d+)
            >
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
    def decorator(fun):
        unsafe_hook_command(name, fun)
        @plugins.finalizer
        def finalizer():
            unsafe_unhook_command(name, fun)
        return fun
    return decorator
