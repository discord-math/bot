import asyncio
from datetime import datetime, timedelta
import enum
import logging
import re
from typing import TYPE_CHECKING, Dict, Iterable, List, Literal, Optional, Set, Union, cast

import discord
from discord import AllowedMentions, Guild, Member, Message
from discord.abc import Snowflake
from discord.ext.commands import Greedy, group
import discord.utils
from sqlalchemy import ARRAY, TEXT, BigInteger, Enum, select
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.acl import privileged
from bot.client import client
from bot.commands import Context, cleanup, plugin_command
import bot.message_tracker
import plugins
import plugins.phish
import plugins.tickets
import util.db.kv
from util.discord import (
    CodeBlock,
    DurationConverter,
    Inline,
    InvocationError,
    PartialRoleConverter,
    PlainItem,
    UserError,
    chunk_messages,
    format,
    retry,
)


logger = logging.getLogger(__name__)

registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)


class MatchType(enum.Enum):
    SUBSTRING = "substring"
    WORD = "word"
    REGEX = "regex"


class ActionType(enum.Enum):
    DELETE = "delete"
    NOTE = "note"
    MUTE = "mute"
    KICK = "kick"
    BAN = "ban"


@registry.mapped
class Rule:
    __tablename__ = "rules"
    __table_args__ = {"schema": "automod"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    keywords: Mapped[List[str]] = mapped_column(ARRAY(TEXT), nullable=False)
    type: Mapped[MatchType] = mapped_column(Enum(MatchType, schema="automod"), nullable=False)
    action: Mapped[Optional[ActionType]] = mapped_column(Enum(ActionType, schema="automod"))
    action_duration: Mapped[Optional[timedelta]] = mapped_column(INTERVAL)

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            keywords: List[str],
            type: MatchType,
            id: int = ...,
            action: Optional[ActionType] = ...,
            action_duration: Optional[timedelta] = ...,
        ) -> None:
            ...


@registry.mapped
class ExemptRole:
    __tablename__ = "exempt_roles"
    __table_args__ = {"schema": "automod"}

    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    if TYPE_CHECKING:

        def __init__(self, *, role_id: int) -> None:
            ...


active_rules: Dict[int, Rule]
regex: re.Pattern[str]
exempt_roles: Set[int]


def rule_to_regex(rule: Rule) -> str:
    if rule.type == MatchType.SUBSTRING:
        return r"|".join(re.escape(keyword) for keyword in rule.keywords)
    elif rule.type == MatchType.WORD:
        return r"|".join(r"\b" + re.escape(keyword) + r"\b" for keyword in rule.keywords)
    else:
        return r"|".join(r"(?:" + keyword + r")" for keyword in rule.keywords)


async def rehash_rules(session: AsyncSession) -> None:
    global active_rules, regex, exempt_roles
    stmt = select(Rule).where(Rule.action != None).order_by(Rule.id)
    rules = (await session.execute(stmt)).scalars()
    stmt = select(ExemptRole.role_id)
    roles = (await session.execute(stmt)).scalars()

    active_rules = {rule.id: rule for rule in rules}
    exempt_roles = set(roles)

    parts: List[str] = []
    for rule in active_rules.values():
        parts.append(r"(?P<_" + str(rule.id) + r">" + rule_to_regex(rule) + r")")
    regex = re.compile("|".join(parts), re.I) if parts else re.compile("(?!)")


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
            await plugins.tickets.create_note(
                session, serialize_note({index: 1}), modid=client.user.id, targetid=target_id, approved=True
            )
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


def fork_ban_user(guild: Guild, user: Snowflake, duration: Optional[timedelta], reason: str) -> None:
    if duration is not None:
        reason = "Banned by {}: {} seconds, {}".format(
            client.user.id if client.user else None, int(duration.total_seconds()), reason
        )
    asyncio.create_task(do_ban_user(guild, user, reason), name=format("Automod ban {!m}", user))


async def do_time_out(member: Member, until: datetime, reason: str) -> None:
    try:
        await retry(lambda: member.edit(timed_out_until=until, reason=reason), attempts=10)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.error(format("Could not mute member {!m} ({})", member, reason), exc_info=True)


def fork_time_out(member: Member, duration: timedelta, reason: str) -> None:
    until = discord.utils.utcnow() + duration
    asyncio.create_task(do_time_out(member, until, reason), name=format("Automod mute {!m}", member))


def phish_match(msg: Message, text: str) -> None:
    assert msg.guild is not None
    logger.info("Message {} contains phishing: {}".format(msg.id, text))
    if isinstance(msg.author, Member):
        if any(role.id in exempt_roles for role in msg.author.roles):
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
        if msg.guild is None:
            continue
        if msg.author.bot:
            continue

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
                else:
                    continue
                logger.info("Message {} matches pattern {}".format(msg.id, index))
                if isinstance(msg.author, Member):
                    if any(role.id in exempt_roles for role in msg.author.roles):
                        continue
                if (rule := active_rules.get(index)) is not None:
                    reason = "Automatic action: message matches pattern {}".format(index)
                    duration = rule.action_duration

                    if rule.action == ActionType.DELETE:
                        fork_delete_message(msg)

                    elif rule.action == ActionType.NOTE:
                        fork_delete_message(msg)
                        fork_create_automod_note(msg.author.id, index)

                    elif rule.action == ActionType.MUTE:
                        fork_delete_message(msg)
                        if isinstance(msg.author, Member):
                            if duration is None:
                                duration = timedelta(days=28)
                            fork_time_out(msg.author, duration, reason)

                    elif rule.action == ActionType.KICK:
                        fork_delete_message(msg)
                        fork_kick_user(msg.guild, msg.author, reason)

                    elif rule.action == ActionType.BAN:
                        fork_delete_message(msg)
                        fork_ban_user(msg.guild, msg.author, duration, reason)
        except:
            logger.error("Could not automod scan {}".format(msg.jump_url), exc_info=True)


@plugins.init
async def init() -> None:
    await util.db.init(util.db.get_ddl(CreateSchema("automod"), registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)

        if conf.index is not None:
            for i in range(cast(int, conf.index)):
                if conf[i, "keyword"] is None or conf[i, "type"] is None:
                    continue

                type = MatchType(conf[i, "type"])
                if i not in cast(List[int], conf.active):
                    action = None
                else:
                    action = ActionType(conf[i, "action"])
                if (seconds := cast(Optional[float], conf[i, "duration"])) is not None:
                    action_duration = timedelta(seconds=seconds)
                else:
                    action_duration = None
                session.add(
                    Rule(
                        id=i,
                        keywords=cast(List[str], conf[i, "keyword"]),
                        type=type,
                        action=action,
                        action_duration=action_duration,
                    )
                )
            for role_id in cast(List[int], conf.exempt_roles):
                session.add(ExemptRole(role_id=role_id))
            await session.commit()
            conf.index = None
            await conf

        if conf.exempt_roles is not None:
            for role_id in cast(List[int], conf.exempt_roles):
                session.add(ExemptRole(role_id=role_id))
            await session.commit()
            conf.exempt_roles = None
            await conf

        await rehash_rules(session)

    await bot.message_tracker.subscribe(__name__, None, process_messages, missing=True, retroactive=False)

    async def unsubscribe() -> None:
        await bot.message_tracker.unsubscribe(__name__, None)

    plugins.finalizer(unsubscribe)


@plugin_command
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
    for id in exempt_roles:
        output.append("{!M}")
        role = discord.utils.find(lambda r: r.id == id, ctx.guild.roles if ctx.guild is not None else ())
        if role is not None:
            output.append(format("{!M}({!i} {!i})", role, role.name, role.id))
        else:
            output.append(format("{!M}({!i})", id, id))
    await ctx.send("Roles exempt from automod: {}".format(", ".join(output)), allowed_mentions=AllowedMentions.none())


@automod_exempt.command("add")
@privileged
async def automod_exempt_add(ctx: Context, role: PartialRoleConverter) -> None:
    """Make a role exempt from automod."""
    async with sessionmaker() as session:
        session.add(ExemptRole(role_id=role.id))
        await session.commit()
        await rehash_rules(session)
        await ctx.send(format("{!M} is now exempt from automod", role), allowed_mentions=AllowedMentions.none())


@automod_exempt.command("remove")
@privileged
async def automod_exempt_remove(ctx: Context, role: PartialRoleConverter) -> None:
    """Make a role not exempt from automod."""
    async with sessionmaker() as session:
        await session.delete(await session.get(ExemptRole, role.id))
        await session.commit()
        await rehash_rules(session)
        await ctx.send(format("{!M} is no longer exempt from automod", role), allowed_mentions=AllowedMentions.none())


@automod_command.command("list")
@privileged
async def automod_list(ctx: Context) -> None:
    """List all automod patterns (CW)."""
    items = [PlainItem("**Automod patterns**:\n")]
    for rule in active_rules.values():
        if rule.action is None:
            continue
        if rule.action in [ActionType.MUTE, ActionType.BAN]:
            duration = " for {} seconds".format(rule.action_duration) if rule.action_duration else " permanently"
        else:
            duration = ""
        items.append(
            PlainItem(
                "**{}**: {} {} -> {}{}\n".format(
                    rule.id,
                    rule.type.value,
                    ", ".join(format("||{!i}||", keyword) for keyword in rule.keywords),
                    rule.action.value,
                    duration,
                )
            )
        )

    for content, _ in chunk_messages(items):
        await ctx.send(content)


@automod_command.command("add")
@privileged
async def automod_add(
    ctx: Context, kind: Literal["substring", "word", "regex"], patterns: Greedy[Union[CodeBlock, Inline, str]]
) -> None:
    """
    Add an automod pattern with one or more keywords.
    "substring" means the patterns will be matched anywhere in a message;
    "word" means the patterns have to match a separate word;
    "regex" means the patterns are case-insensitive regexes (use (?-i) to enable case sensitivity)
    """
    await ctx.message.delete()
    ctx.send = ctx.channel.send  # type: ignore # Undoing the effect of cleanup
    if len(patterns) == 0:
        raise InvocationError("Provide at least one pattern")

    kind_enum = MatchType(kind)

    keywords: List[str] = []
    for pattern in patterns:
        if isinstance(pattern, (CodeBlock, Inline)):
            pattern = pattern.text
        if kind_enum == MatchType.REGEX:
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

    async with sessionmaker() as session:
        stmt = select(Rule).where(Rule.keywords == keywords, Rule.type == kind_enum, Rule.action == None).limit(1)
        if not (rule := (await session.execute(stmt)).scalar()):
            rule = Rule(keywords=keywords, type=kind_enum, action=None)
            session.add(rule)
            await session.commit()
            await rehash_rules(session)

    await ctx.send(
        "Added {} as pattern **{}** with no action".format(
            ", ".join(format("||{!i}||", keyword) for keyword in keywords), rule.id
        )
    )


@automod_command.command("remove")
@privileged
async def automod_remove(ctx: Context, number: int) -> None:
    """Remove an automod pattern by ID."""
    async with sessionmaker() as session:
        if not (rule := await session.get(Rule, number)):
            raise UserError("No such pattern")
        rule.action = None
        await session.commit()
        await rehash_rules(session)
        await ctx.send(
            "Removed {} {}".format(rule.type, ", ".join(format("||{!i}||", keyword) for keyword in rule.keywords))
        )


@automod_command.command("action")
@privileged
async def automod_action(
    ctx: Context,
    number: int,
    action: Literal["delete", "note", "mute", "kick", "ban"],
    duration: Optional[DurationConverter],
) -> None:
    """Assign an action to an automod pattern. (All actions imply deletion)."""
    async with sessionmaker() as session:
        if not (rule := await session.get(Rule, number)):
            raise UserError("No such pattern")
        rule.action = ActionType(action)
        rule.action_duration = duration
        await session.commit()
        await rehash_rules(session)
        await ctx.send("\u2705")
