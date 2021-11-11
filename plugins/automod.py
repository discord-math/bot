import aiohttp
import asyncio
import logging
import re
import discord
import discord.utils
from typing import List, Dict, Tuple, Optional, Union, Literal, Iterable, Awaitable, Protocol, overload, cast
import util.db.kv
import util.discord
import util.frozen_list
import discord_client
import plugins.commands
import plugins.tickets
import plugins.phish
import plugins.message_tracker

class AutomodConf(Protocol, Awaitable[None]):
    active: util.frozen_list.FrozenList[int]
    index: int
    mute_role: int
    exempt_roles: util.frozen_list.FrozenList[int]

    @overload
    def __getitem__(self, k: Tuple[int, Literal["keyword"]]) -> Optional[util.frozen_list.FrozenList[str]]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["type"]]) -> Optional[Literal["substring", "word", "regex"]]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["action"]]
        ) -> Optional[Literal["delete", "note", "mute", "kick", "ban"]]: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["keyword"]], v: Optional[util.frozen_list.FrozenList[str]]) -> None: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["type"]], v: Optional[Literal["substring", "word", "regex"]]
        ) -> None: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["action"]],
        v: Optional[Literal["delete", "note", "mute", "kick", "ban"]]) -> None: ...

logger = logging.getLogger(__name__)

conf: AutomodConf

def to_regex(kind: Literal["substring", "word", "regex"], keyword: str) -> str:
    if kind == "substring":
        return re.escape(keyword)
    elif kind == "word":
        return r"\b{}\b".format(re.escape(keyword))
    else:
        return r"(?:{})".format(keyword)

regex: re.Pattern[str]
def generate_regex() -> None:
    global regex
    parts = []
    for i in conf.active:
        if (keywords := conf[i, "keyword"]) is not None and (kind := conf[i, "type"]) is not None:
            parts.append(r"(?P<_{}>{})".format(i, r"|".join(to_regex(kind, keyword) for keyword in keywords)))
    if len(parts) > 0:
        regex = re.compile("|".join(parts), re.I)
    else:
        regex = re.compile("(?!)")

def parse_note(text: Optional[str]) -> Dict[int, int]:
    data = {}
    if text is not None:
        for line in text.splitlines()[1:]:
            words = line.split()
            if len(words) == 5 and words[0] == "pattern" and words[2] == "matched" and words[4] == "times":
                try:
                    data[int(words[1])] = int(words[3])
                except ValueError:
                    pass
    return data

def serialize_note(data: Dict[int, int]) -> str:
    return "Automod:\n" + "\n".join("pattern {} matched {} times".format(index, value) for index, value in data.items())

async def create_automod_note(target_id: int, index: int) -> None:
    async with plugins.tickets.sessionmaker() as session:
        assert discord_client.client.user is not None
        notes = await plugins.tickets.find_notes_prefix(session, "Automod:\n",
            modid=discord_client.client.user.id, targetid=target_id)
        if len(notes) == 0:
            await plugins.tickets.create_note(session, serialize_note({index: 1}),
                modid=discord_client.client.user.id, targetid=target_id)
        else:
            data = parse_note(notes[-1].comment)
            data[index] = 1 + data.get(index, 0)
            notes[-1].comment = serialize_note(data)
        async with plugins.tickets.Ticket.publish_all(session):
            await session.commit()
        await session.commit()

URL_regex: re.Pattern[str] = re.compile(r"https?://([^/]*)/?\S*", re.I)

async def phish_match(msg: discord.Message, text: str) -> None:
    assert msg.guild is not None
    try:
        reason = "Automatic action: found phishing domain: {}".format(text)
        await asyncio.gather(msg.delete(),
            msg.guild.ban(msg.author, reason=reason, delete_message_days=0))
    except (discord.Forbidden, discord.NotFound):
        logger.error("Could not moderate {}".format(msg.jump_url), exc_info=True)

async def resolve_link(msg: discord.Message, link: str) -> None:
    if (target := await plugins.phish.resolve_link(link)) is not None:
        if (match := URL_regex.match(target)) is not None:
            if match.group(1) in plugins.phish.domains:
                await phish_match(msg, util.discord.format("{!i} -> {!i}", link, match.group(1)))

async def process_messages(msgs: Iterable[discord.Message]) -> None:
    for msg in msgs:
        if msg.guild is None: continue
        if msg.author.bot: continue

        try:
            match: Optional[re.Match[str]]
            resolve_links = set()
            for match in URL_regex.finditer(msg.content):
                if match.group(1).lower() in plugins.phish.domains:
                    await phish_match(msg, util.discord.format("{!i}", match.group(1)))
                    break
                elif plugins.phish.should_resolve_domain(match.group(1)):
                    resolve_links.add(match.group(0))
            for link in resolve_links:
                asyncio.create_task(resolve_link(msg, link))

            if (match := regex.search(msg.content)) is not None:
                for key, value in match.groupdict().items():
                    if value is not None:
                        index = int(key[1:])
                        break
                else: continue
                logger.info("Message {} matches pattern {}".format(msg.id, index))
                if isinstance(msg.author, discord.Member):
                    if any(role.id in conf.exempt_roles for role in msg.author.roles):
                        continue
                if (action := conf[index, "action"]) is not None:
                    try:
                        reason = "Automatic action: message matches pattern {}".format(index)

                        if action == "delete":
                            await msg.delete()

                        elif action == "note":
                            await asyncio.gather(msg.delete(),
                                create_automod_note(msg.author.id, index))

                        elif action == "mute":
                            if isinstance(msg.author, discord.Member):
                                await asyncio.gather(msg.delete(),
                                    msg.author.add_roles(discord.Object(conf.mute_role), reason=reason))
                            else:
                                await msg.delete()

                        elif action == "kick":
                            await asyncio.gather(msg.delete(),
                                msg.guild.kick(msg.author, reason=reason))

                        elif action == "ban":
                            await asyncio.gather(msg.delete(),
                                msg.guild.ban(msg.author, reason=reason, delete_message_days=0))

                    except (discord.HTTPException, AssertionError):
                        logger.error("Could not moderate {}".format(msg.jump_url), exc_info=True)
        except:
            logger.error("Could not automod scan {}".format(msg.jump_url), exc_info=True)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(AutomodConf, await util.db.kv.load(__name__))
    if conf.index is None: conf.index = 1
    if conf.active is None: conf.active = util.frozen_list.FrozenList()
    if conf.exempt_roles is None: conf.exempt_roles = util.frozen_list.FrozenList()
    for i in range(conf.index):
        if isinstance(keyword := conf[i, "keyword"], str):
            conf[i, "keyword"] = util.frozen_list.FrozenList((keyword,))

    await conf
    generate_regex()
    await plugins.message_tracker.subscribe(__name__, None, process_messages, missing=True, retroactive=False)
    @plugins.finalizer
    async def unsubscribe() -> None:
        await plugins.message_tracker.unsubscribe(__name__, None)

@plugins.commands.cleanup
@plugins.commands.command("automod", cls=discord.ext.commands.Group)
@plugins.privileges.priv("mod")
async def automod_command(ctx: discord.ext.commands.Context) -> None:
    """Manage automod."""
    pass

@automod_command.group("exempt", invoke_without_command=True)
async def automod_exempt(ctx: discord.ext.commands.Context) -> None:
    """Manage roles exempt from automod."""
    output = []
    for id in conf.exempt_roles:
        role = discord.utils.find(lambda r: r.id == id, ctx.guild.roles if ctx.guild is not None else ())
        if role is not None:
            output.append(util.discord.format("{!M}({!i} {!i})", role, role.name, role.id))
        else:
            output.append(util.discord.format("{!M}({!i})", id, id))
    await ctx.send("Roles exempt from automod: {}".format(", ".join(output)),
        allowed_mentions=discord.AllowedMentions.none())

@automod_exempt.command("add")
async def automod_exempt_add(ctx: discord.ext.commands.Context, role: util.discord.PartialRoleConverter) -> None:
    """Make a role exempt from automod."""
    roles = set(conf.exempt_roles)
    roles.add(role.id)
    conf.exempt_roles = util.frozen_list.FrozenList(roles)
    await conf
    await ctx.send(util.discord.format("{!M} is now exempt from automod", role),
        allowed_mentions=discord.AllowedMentions.none())

@automod_exempt.command("remove")
async def automod_exempt_remove(ctx: discord.ext.commands.Context, role: util.discord.PartialRoleConverter) -> None:
    """Make a role not exempt from automod."""
    roles = set(conf.exempt_roles)
    roles.discard(role.id)
    conf.exempt_roles = util.frozen_list.FrozenList(roles)
    await conf
    await ctx.send(util.discord.format("{!M} is no longer exempt from automod", role),
        allowed_mentions=discord.AllowedMentions.none())

@automod_command.command("list")
async def automod_list(ctx: discord.ext.commands.Context) -> None:
    """List all automod patterns (CW)."""
    output = "**Automod patterns**:\n"
    for i in conf.active:
        if (keywords := conf[i, "keyword"]) is not None and (kind := conf[i, "type"]) is not None and (
            action := conf[i, "action"]) is not None:
            text = "**{}**: {} {} -> {}\n".format(i, kind,
                ", ".join(util.discord.format("||{!i}||", keyword) for keyword in keywords), action)

            if len(output) + len(text) > 2000:
                if len(output) > 0:
                    await ctx.send(output)
                output = text
            else:
                output += text
    if len(output) > 0:
        await ctx.send(output)

@automod_command.command("add")
async def automod_add(ctx: discord.ext.commands.Context, kind: Literal["substring", "word", "regex"],
    patterns: discord.ext.commands.Greedy[Union[util.discord.CodeBlock, util.discord.Inline, str]]) -> None:
    """
        Add an automod pattern with one or more keywords.
        "substring" means the patterns will be matched anywhere in a message;
        "word" means the patterns have to match a separate word;
        "regex" means the patterns are case-insensitive regexes (use (?-i) to enable case sensitivity)
    """
    await ctx.message.delete()
    ctx.send = ctx.channel.send # type: ignore # Undoing the effect of cleanup
    if len(patterns) == 0:
        raise util.discord.InvocationError("Provide at least one pattern")
    keywords: List[str] = []
    for pattern in patterns:
        if isinstance(pattern, (util.discord.CodeBlock, util.discord.Inline)):
            pattern = pattern.text
        if kind == "regex":
            try:
                regex = re.compile(pattern)
            except Exception as exc:
                raise util.discord.UserError("Could not compile regex: {}".format(exc))
            if regex.search("") is not None:
                raise util.discord.UserError("Regex matches empty string, that's probably not good")
        else:
            if pattern == "":
                raise util.discord.UserError("The pattern is empty, that's probably not good")
        keywords.append(pattern)

    for i in range(conf.index):
        if conf[i, "keyword"] == keywords and conf[i, "type"] == kind and conf[i, "action"] == None:
            break
    else:
        i = conf.index
        conf.index += 1
        conf[i, "keyword"] = util.frozen_list.FrozenList(keywords)
        conf[i, "type"] = kind
        conf[i, "action"] = None
        await conf
    await ctx.send("Added as pattern **{}** with no action".format(i))

@automod_command.command("remove")
async def automod_remove(ctx: discord.ext.commands.Context, number: int) -> None:
    """Remove an automod pattern by ID."""
    if (keywords := conf[number, "keyword"]) is not None and (kind := conf[number, "type"]) is not None:
        conf[number, "action"] = None
    active = set(conf.active)
    active.discard(number)
    conf.active = util.frozen_list.FrozenList(active)
    await conf
    generate_regex()
    if keywords is not None and kind is not None:
        await ctx.send("Removed {} {}".format(kind,
            ", ".join(util.discord.format("||{!i}||", keyword) for keyword in keywords)))
    else:
        await ctx.send("No such pattern")

@automod_command.command("action")
async def automod_action(ctx: discord.ext.commands.Context, number: int,
    action: Literal["delete", "note", "mute", "kick", "ban"]) -> None:
    """Assign an action to an automod pattern. (All actions imply deletion)."""
    if conf[number, "keyword"] is None or conf[number, "type"] is None:
        raise util.discord.UserError("No such pattern")
    conf[number, "action"] = action
    active = set(conf.active)
    active.add(number)
    conf.active = util.frozen_list.FrozenList(active)
    await conf
    generate_regex()
    await ctx.send("\u2705")
