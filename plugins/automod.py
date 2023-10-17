import asyncio
from datetime import datetime, timedelta
import logging
import re
from typing import Awaitable, Dict, Iterable, List, Literal, Optional, Protocol, Set, Tuple, Union, cast, overload

import discord
from discord import AllowedMentions, Guild, Member, Message
from discord.abc import Snowflake
from discord.ext.commands import Greedy
import discord.utils

from bot.acl import privileged
from bot.client import client
from bot.commands import Context, cleanup, group
import bot.message_tracker
import plugins
import plugins.phish
import plugins.tickets
import util.db.kv
from util.discord import (CodeBlock, DurationConverter, Inline, InvocationError, PartialRoleConverter, PlainItem,
    UserError, chunk_messages, format, retry)
from util.frozen_list import FrozenList

class AutomodConf(Awaitable[None], Protocol):
    active: FrozenList[int]
    index: int
    exempt_roles: FrozenList[int]

    @overload
    def __getitem__(self, k: Tuple[int, Literal["keyword"]]) -> Optional[FrozenList[str]]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["type"]]) -> Optional[Literal["substring", "word", "regex"]]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["action"]]
        ) -> Optional[Literal["delete", "note", "mute", "kick", "ban"]]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["duration"]]) -> Optional[float]: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["keyword"]], v: Optional[FrozenList[str]]) -> None: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["type"]], v: Optional[Literal["substring", "word", "regex"]]
        ) -> None: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["action"]],
        v: Optional[Literal["delete", "note", "mute", "kick", "ban"]]) -> None: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["duration"]], v: Optional[float]) -> None: ...

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

async def do_create_automod_note(target_id: int, index: int) -> None:
    async with plugins.tickets.sessionmaker() as session:
        assert client.user is not None
        notes = await plugins.tickets.find_notes_prefix(session, "Automod:\n", modid=client.user.id, targetid=target_id)
        if len(notes) == 0:
            await plugins.tickets.create_note(session, serialize_note({index: 1}), modid=client.user.id,
                targetid=target_id, approved=True)
        else:
            data = parse_note(notes[-1].comment)
            data[index] = 1 + data.get(index, 0)
            notes[-1].comment = serialize_note(data)
        async with plugins.tickets.Ticket.publish_all(session):
            await session.commit()
        await session.commit()

def fork_create_automod_note(target_id: int, index: int) -> None:
    asyncio.create_task(do_create_automod_note(target_id, index), name=format("Automod note {!m}", target_id))

URL_regex: re.Pattern[str] = re.compile(r"https?://([^/]*)/?\S*", re.I)

async def do_delete_message(msg: Message) -> None:
    try:
        await retry(lambda: msg.delete(), attempts=10)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.error("Could not delete message {}".format(msg.jump_url), exc_info=True)

def fork_delete_message(msg: Message) -> None:
    asyncio.create_task(do_delete_message(msg), name="Automod cleanup {}".format(msg.id))

async def do_kick_user(guild: Guild, user: Snowflake, reason: str) -> None:
    try:
        await retry(lambda: guild.kick(user, reason=reason), attempts=10)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.error(format("Could not kick user {!m} ({})", user, reason), exc_info=True)

def fork_kick_user(guild: Guild, user: Snowflake, reason: str) -> None:
    asyncio.create_task(do_kick_user(guild, user, reason), name=format("Automod kick {!m}", user))

async def do_ban_user(guild: Guild, user: Snowflake, reason: str) -> None:
    try:
        await retry(lambda: guild.ban(user, reason=reason, delete_message_days=0), attempts=10)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.error(format("Could not ban user {!m} ({})", user, reason), exc_info=True)

def fork_ban_user(guild: Guild, user: Snowflake, duration: Optional[float], reason: str) -> None:
    if duration is not None:
        reason = "Banned by {}: {} seconds, {}".format(client.user.id if client.user else None, int(duration), reason)
    asyncio.create_task(do_ban_user(guild, user, reason), name=format("Automod ban {!m}", user))

async def do_time_out(member: Member, until: datetime, reason: str) -> None:
    try:
        await retry(lambda: member.edit(timed_out_until=until, reason=reason), attempts=10)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.error(format("Could not mute member {!m} ({})", member, reason), exc_info=True)

def fork_time_out(member: Member, duration: float, reason: str) -> None:
    until = discord.utils.utcnow() + timedelta(seconds=duration)
    asyncio.create_task(do_time_out(member, until, reason), name=format("Automod mute {!m}", member))

def phish_match(msg: Message, text: str) -> None:
    assert msg.guild is not None
    logger.info("Message {} contains phishing: {}".format(msg.id, text))
    if isinstance(msg.author, Member):
        if any(role.id in conf.exempt_roles for role in msg.author.roles):
            return
        fork_ban_user(msg.guild, msg.author, None, "Automatic action: found phishing domain: {}".format(text))
    fork_delete_message(msg)

async def resolve_link(msg: Message, link: str) -> None:
    if (target := await plugins.phish.resolve_link(link)) is not None:
        if (match := URL_regex.match(target)) is not None:
            if plugins.phish.is_bad_domain(match.group(1)):
                phish_match(msg, format("{!i} -> {!i}", link, match.group(1)))

async def process_messages(msgs: Iterable[Message]) -> None:
    for msg in msgs:
        if msg.guild is None: continue
        if msg.author.bot: continue

        try:
            match: Optional[re.Match[str]]
            resolve_links: Set[str] = set()
            for match in URL_regex.finditer(msg.content):
                if plugins.phish.is_bad_domain(match.group(1).lower()):
                    phish_match(msg, format("{!i}", match.group(1)))
                    break
                elif plugins.phish.should_resolve_domain(match.group(1)):
                    resolve_links.add(match.group(0))
            for link in resolve_links:
                asyncio.create_task(resolve_link(msg, link), name="phish link resolver")

            if (match := regex.search(msg.content)) is not None:
                for key, value in match.groupdict().items():
                    if value is not None:
                        index = int(key[1:])
                        break
                else: continue
                logger.info("Message {} matches pattern {}".format(msg.id, index))
                if isinstance(msg.author, Member):
                    if any(role.id in conf.exempt_roles for role in msg.author.roles):
                        continue
                if (action := conf[index, "action"]) is not None:
                    reason = "Automatic action: message matches pattern {}".format(index)
                    duration = conf[index, "duration"]

                    if action == "delete":
                        fork_delete_message(msg)

                    elif action == "note":
                        fork_delete_message(msg)
                        fork_create_automod_note(msg.author.id, index)

                    elif action == "mute":
                        fork_delete_message(msg)
                        if isinstance(msg.author, Member):
                            if duration is None:
                                duration = 86400 * 28
                            fork_time_out(msg.author, duration, reason)

                    elif action == "kick":
                        fork_delete_message(msg)
                        fork_kick_user(msg.guild, msg.author, reason)

                    elif action == "ban":
                        fork_delete_message(msg)
                        fork_ban_user(msg.guild, msg.author, duration, reason)
        except:
            logger.error("Could not automod scan {}".format(msg.jump_url), exc_info=True)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(AutomodConf, await util.db.kv.load(__name__))
    if conf.index is None: conf.index = 1
    if conf.active is None: conf.active = FrozenList()
    if conf.exempt_roles is None: conf.exempt_roles = FrozenList()
    for i in range(conf.index):
        if isinstance(keyword := conf[i, "keyword"], str):
            conf[i, "keyword"] = FrozenList((keyword,))

    await conf
    generate_regex()
    await bot.message_tracker.subscribe(__name__, None, process_messages, missing=True, retroactive=False)
    async def unsubscribe() -> None:
        await bot.message_tracker.unsubscribe(__name__, None)
    plugins.finalizer(unsubscribe)

@cleanup
@group("automod")
@privileged
async def automod_command(ctx: Context) -> None:
    """Manage automod."""
    pass

@automod_command.group("exempt", invoke_without_command=True)
@privileged
async def automod_exempt(ctx: Context) -> None:
    """Manage roles exempt from automod."""
    output = []
    for id in conf.exempt_roles:
        role = discord.utils.find(lambda r: r.id == id, ctx.guild.roles if ctx.guild is not None else ())
        if role is not None:
            output.append(format("{!M}({!i} {!i})", role, role.name, role.id))
        else:
            output.append(format("{!M}({!i})", id, id))
    await ctx.send("Roles exempt from automod: {}".format(", ".join(output)),
        allowed_mentions=AllowedMentions.none())

@automod_exempt.command("add")
@privileged
async def automod_exempt_add(ctx: Context, role: PartialRoleConverter) -> None:
    """Make a role exempt from automod."""
    roles = set(conf.exempt_roles)
    roles.add(role.id)
    conf.exempt_roles = FrozenList(roles)
    await conf
    await ctx.send(format("{!M} is now exempt from automod", role),
        allowed_mentions=AllowedMentions.none())

@automod_exempt.command("remove")
@privileged
async def automod_exempt_remove(ctx: Context, role: PartialRoleConverter) -> None:
    """Make a role not exempt from automod."""
    roles = set(conf.exempt_roles)
    roles.discard(role.id)
    conf.exempt_roles = FrozenList(roles)
    await conf
    await ctx.send(format("{!M} is no longer exempt from automod", role),
        allowed_mentions=AllowedMentions.none())

@automod_command.command("list")
@privileged
async def automod_list(ctx: Context) -> None:
    """List all automod patterns (CW)."""
    items = [PlainItem("**Automod patterns**:\n")]
    for i in conf.active:
        if (keywords := conf[i, "keyword"]) is not None and (kind := conf[i, "type"]) is not None and (
            action := conf[i, "action"]) is not None:
            if action in ["mute", "ban"]:
                duration = " for {} seconds".format(conf[i, "duration"]) if conf[i, "duration"] else " permanently"
            else:
                duration = ""
            items.append(PlainItem("**{}**: {} {} -> {}{}\n".format(i, kind,
                ", ".join(format("||{!i}||", keyword) for keyword in keywords), action, duration)))

    for content, _ in chunk_messages(items):
        await ctx.send(content)

@automod_command.command("add")
@privileged
async def automod_add(ctx: Context, kind: Literal["substring", "word", "regex"],
    patterns: Greedy[Union[CodeBlock, Inline, str]]) -> None:
    """
        Add an automod pattern with one or more keywords.
        "substring" means the patterns will be matched anywhere in a message;
        "word" means the patterns have to match a separate word;
        "regex" means the patterns are case-insensitive regexes (use (?-i) to enable case sensitivity)
    """
    await ctx.message.delete()
    ctx.send = ctx.channel.send # type: ignore # Undoing the effect of cleanup
    if len(patterns) == 0:
        raise InvocationError("Provide at least one pattern")
    keywords: List[str] = []
    for pattern in patterns:
        if isinstance(pattern, (CodeBlock, Inline)):
            pattern = pattern.text
        if kind == "regex":
            try:
                regex = re.compile(pattern)
            except Exception as exc:
                raise UserError("Could not compile regex: {}".format(exc))
            if regex.search("") is not None:
                raise UserError("Regex matches empty string, that's probably not good")
        else:
            if pattern == "":
                raise UserError("The pattern is empty, that's probably not good")
        keywords.append(pattern)

    for i in range(conf.index):
        if conf[i, "keyword"] == keywords and conf[i, "type"] == kind and conf[i, "action"] == None:
            break
    else:
        i = conf.index
        conf.index += 1
        conf[i, "keyword"] = FrozenList(keywords)
        conf[i, "type"] = kind
        conf[i, "action"] = None
        await conf
    await ctx.send("Added {} as pattern **{}** with no action".format(
        ", ".join(format("||{!i}||", keyword) for keyword in keywords), i))

@automod_command.command("remove")
@privileged
async def automod_remove(ctx: Context, number: int) -> None:
    """Remove an automod pattern by ID."""
    keywords = conf[number, "keyword"]
    kind = conf[number, "type"]
    if keywords is not None and kind is not None:
        conf[number, "action"] = None
    active = set(conf.active)
    active.discard(number)
    conf.active = FrozenList(active)
    await conf
    generate_regex()
    if keywords is not None and kind is not None:
        await ctx.send("Removed {} {}".format(kind,
            ", ".join(format("||{!i}||", keyword) for keyword in keywords)))
    else:
        await ctx.send("No such pattern")

@automod_command.command("action")
@privileged
async def automod_action(ctx: Context, number: int, action: Literal["delete", "note", "mute", "kick", "ban"],
    duration: Optional[DurationConverter]) -> None:
    """Assign an action to an automod pattern. (All actions imply deletion)."""
    if conf[number, "keyword"] is None or conf[number, "type"] is None:
        raise UserError("No such pattern")
    conf[number, "action"] = action
    conf[number, "duration"] = None if duration is None else duration.total_seconds()
    active = set(conf.active)
    active.add(number)
    conf.active = FrozenList(active)
    await conf
    generate_regex()
    await ctx.send("\u2705")
