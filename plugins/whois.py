from __future__ import annotations

import asyncio
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
import enum
from functools import total_ordering
import heapq
import itertools
import logging
import re
import threading
from typing import Dict, Generic, Iterable, Iterator, List, Literal, Optional, Sequence, Set, Tuple, TypeVar, Union

import datrie
import discord
from discord import Embed, Guild, Interaction, Member, RawMemberRemoveEvent, User
from discord.app_commands import Choice, default_permissions, guild_only
from discord.utils import snowflake_time
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.client import client
from bot.cogs import Cog, cog
from bot.interactions import command
import plugins.log
import plugins.tickets
from util.discord import PlainItem, chunk_messages, format

logger = logging.getLogger(__name__)

@total_ordering
class InfixType(enum.Enum):
    EXACT = 0
    PREFIX = 1
    INFIX = 2

    def __lt__(self, other: InfixType) -> bool:
        return self.value < other.value

InfixRank = Union[
    Tuple[Literal[InfixType.EXACT]],
    Tuple[Literal[InfixType.PREFIX], int],
    Tuple[Literal[InfixType.INFIX], int]]

T_co = TypeVar("T_co", covariant=True)

@dataclass(order=True)
class InfixCandidate(Generic[T_co]):
    rank: InfixRank
    match: T_co = field(compare=False)

class IdTrie:
    trie: datrie.Trie
    lock: threading.Lock

    def __init__(self):
        self.trie = datrie.Trie("0123456789")

    def insert(self, value: int) -> None:
        self.trie[str(value)] = value

    def delete(self, value: int) -> None:
        self.trie.pop(str(value), None)

    def lookup(self, input: str) -> Iterable[InfixCandidate[int]]:
        return sorted(InfixCandidate((InfixType.EXACT,) if input == str(value) else
            (InfixType.PREFIX, len(key) - len(input)), value)
            for key, value in self.trie.items(input))

class InfixTrie:
    # Most common characters to appear in nicknames and usernames
    common_chars = (" #.0123456789_abcdefghijklmnopqrstuvwxyz")
    assert len(common_chars) <= 254
    uncommon_re = re.compile("[^" + re.escape(common_chars) + "]")

    tries: Dict[int, datrie.Trie[List[int]]]
    uncommon: Dict[str, Dict[int, str]]
    lock: threading.Lock

    @classmethod
    def make_trie(cls) -> datrie.Trie: # type: ignore
        return datrie.Trie("\n" + cls.common_chars)

    def __init__(self):
        self.tries = defaultdict(self.make_trie)
        self.uncommon = defaultdict(dict)
        self.lock = threading.Lock()

    def common_key_iter(self, key: str) -> Iterator[Tuple[datrie.Trie, str]]: # type: ignore
        common_key = re.sub(self.uncommon_re, "\n", key)
        for i in range(len(common_key)):
            if common_key[i] != "\n":
                yield self.tries[i], common_key[i:]


    def insert(self, key: str, value: int) -> None:
        key = key.lower()
        with self.lock:
            for ch in re.findall(self.uncommon_re, key):
                self.uncommon[ch][value] = key
            for trie, trie_key in self.common_key_iter(key):
                if (l := trie.get(trie_key)) is not None:
                    l.insert(bisect_left(l, value), value)
                else:
                    trie[trie_key] = [value]

    def delete(self, key: str, value: int) -> None:
        key = key.lower()
        with self.lock:
            for ch in re.findall(self.uncommon_re, key):
                self.uncommon[ch].pop(value, None)
            for trie, trie_key in self.common_key_iter(key):
                if (l := trie.get(trie_key)) is not None:
                    i = bisect_left(l, value)
                    if i < len(l) and l[i] == value:
                        l.pop(i)
                        if not l:
                            del trie[trie_key]

    def lookup(self, input: str) -> Iterator[InfixCandidate[int]]:
        """Returned value might not actually be a match"""
        input = input.lower()
        with self.lock:
            uncommon: Dict[int, str]
            try:
                uncommon = min((d for ch in re.findall(self.uncommon_re, input)
                    if (d := self.uncommon.get(ch)) is not None), key=len)
            except ValueError:
                uncommon = {}

            def uncommon_iter() -> Iterator[InfixCandidate[int]]:
                for value, key in uncommon.items():
                    if key == input:
                        yield InfixCandidate((InfixType.EXACT,), value)
                    elif key.startswith(input):
                        yield InfixCandidate((InfixType.PREFIX, len(key) - len(input)), value)
                    elif input in key:
                        yield InfixCandidate((InfixType.INFIX, len(key) - len(input)), value)

            common_key = re.sub(self.uncommon_re, "\n", input)

            def prefix_iter() -> Iterator[InfixCandidate[List[int]]]:
                for key, values in self.tries[0].items(common_key):
                    if key == input:
                        yield InfixCandidate((InfixType.EXACT,), values)
                    else:
                        yield InfixCandidate((InfixType.PREFIX, len(key) - len(input)), values)

            def infix_iter(i: int) -> Iterator[InfixCandidate[List[int]]]:
                for key, values in self.tries[i].items(common_key):
                    yield InfixCandidate((InfixType.INFIX, i + len(key) - len(input)), values)

            def prefix_iter_sorted(i: int) -> Iterator[InfixCandidate[List[int]]]:
                yield InfixCandidate((InfixType.INFIX, i), [])
                yield from sorted(infix_iter(i))

            for candidate in heapq.merge(sorted(uncommon_iter()), sorted(prefix_iter()),
                *(prefix_iter_sorted(i) for i in range(1, max(self.tries) + 1))):
                if isinstance(candidate.match, int):
                    yield candidate # type:ignore
                else:
                    for value in candidate.match:
                        yield InfixCandidate(candidate.rank, value)

id_trie: IdTrie = IdTrie()
username_trie: InfixTrie = InfixTrie()
displayname_trie: InfixTrie = InfixTrie()
nickname_trie: InfixTrie = InfixTrie()

@plugins.finalizer
def deallocate_tries() -> None:
    global id_trie, username_trie, displayname_trie, nickname_trie
    del id_trie, username_trie, displayname_trie, nickname_trie

@total_ordering
class MatchType(enum.Enum):
    EXACT_ID = 0
    EXACT_USER = 1
    EXACT_NICK = 2
    PREFIX = 3
    INFIX = 4
    EXACT_RECENT_USER = 5
    EXACT_RECENT_NICK = 6
    PREFIX_RECENT = 7
    INFIX_RECENT = 8
    PREFIX_ID = 9

    def __lt__(self, other: MatchType) -> bool:
        return self.value < other.value

@total_ordering
class NickOrUser(enum.Enum):
    USER = 0
    NICK = 1

    def __lt__(self, other: MatchType) -> bool:
        return self.value < other.value

ServerStatus = int
MatchRank = Union[
    Tuple[Literal[MatchType.EXACT_ID]],
    Tuple[Literal[MatchType.EXACT_USER, MatchType.EXACT_NICK], ServerStatus],
    Tuple[Literal[MatchType.PREFIX, MatchType.INFIX], int, NickOrUser, ServerStatus],
    Tuple[Literal[MatchType.EXACT_RECENT_USER, MatchType.EXACT_RECENT_NICK], ServerStatus],
    Tuple[Literal[MatchType.PREFIX_RECENT, MatchType.INFIX_RECENT], int, NickOrUser, ServerStatus],
    Tuple[Literal[MatchType.PREFIX_ID], ServerStatus, int]]

Recent = Tuple[int, str, NickOrUser, bool]

def rank_server_status(m: Optional[Member]) -> ServerStatus:
    return -len(m.roles) if m else 1

def rank_recent_match(text: str, recent: Recent, server_status: ServerStatus) -> MatchRank:
    _, match, nu, infix = recent
    if text == match:
        if nu == NickOrUser.USER:
            return MatchType.EXACT_RECENT_USER, server_status
        else:
            return MatchType.EXACT_RECENT_NICK, server_status
    if infix:
        return MatchType.INFIX_RECENT, len(match) -  len(text), nu, server_status
    else:
        return MatchType.PREFIX_RECENT, len(match) - len(text), nu, server_status

def match_id(match: Union[Member, Recent]):
    return match.id if isinstance(match, Member) else match[0]

@dataclass(order=True)
class Candidate:
    rank: MatchRank
    match: Union[Member, Recent] = field(compare=False)

async def select_candidates(limit: int, input: str, guild: Guild, session: AsyncSession) -> Sequence[Candidate]:
    def id_iter() -> Iterator[Candidate]:
        for candidate in id_trie.lookup(input):
            if (member := guild.get_member(candidate.match)) is not None:
                if candidate.rank[0] == InfixType.EXACT:
                    yield Candidate((MatchType.EXACT_ID,), member)
                else:
                    server_status = rank_server_status(member)
                    yield Candidate((MatchType.PREFIX_ID, server_status, candidate.match), member)

    def username_iter() -> Iterator[Candidate]:
        for candidate in username_trie.lookup(input):
            if (member := guild.get_member(candidate.match)) is not None:
                if input.lower() in (member.name + "#" + member.discriminator).lower():
                    server_status = rank_server_status(member)
                    if candidate.rank[0] == InfixType.EXACT:
                        yield Candidate((MatchType.EXACT_USER, server_status), member)
                    elif candidate.rank[0] == InfixType.PREFIX:
                        yield Candidate((MatchType.PREFIX, candidate.rank[1], NickOrUser.USER, server_status), member)
                    else:
                        yield Candidate((MatchType.INFIX, candidate.rank[1], NickOrUser.USER, server_status), member)

    def displayname_iter() -> Iterator[Candidate]:
        for candidate in displayname_trie.lookup(input):
            if (member := guild.get_member(candidate.match)) is not None:
                if input.lower() in member.display_name.lower():
                    server_status = rank_server_status(member)
                    if candidate.rank[0] == InfixType.EXACT:
                        yield Candidate((MatchType.EXACT_NICK, server_status), member)
                    elif candidate.rank[0] == InfixType.PREFIX:
                        yield Candidate((MatchType.PREFIX, candidate.rank[1], NickOrUser.NICK, server_status), member)
                    else:
                        yield Candidate((MatchType.INFIX, candidate.rank[1], NickOrUser.NICK, server_status), member)

    def nickname_iter() -> Iterator[Candidate]:
        for candidate in nickname_trie.lookup(input):
            if (member := guild.get_member(candidate.match)) is not None:
                if member.nick is not None and input.lower() in member.nick.lower():
                    server_status = rank_server_status(member)
                    if candidate.rank[0] == InfixType.EXACT:
                        yield Candidate((MatchType.EXACT_NICK, server_status), member)
                    elif candidate.rank[0] == InfixType.PREFIX:
                        yield Candidate((MatchType.PREFIX, candidate.rank[1], NickOrUser.NICK, server_status), member)
                    else:
                        yield Candidate((MatchType.INFIX, candidate.rank[1], NickOrUser.NICK, server_status), member)

    def recent_iter(recents: Iterable[Recent]) -> Iterator[Candidate]:
        for recent in recents:
            server_status = rank_server_status(guild.get_member(recent[0]))
            rank = rank_recent_match(input, recent, server_status)
            yield Candidate(rank, recent)

    def unique_candidates(iter: Iterable[Candidate]) -> Iterator[Candidate]:
        ids: Set[int] = set()
        for candidate in iter:
            if (id := match_id(candidate.match)) not in ids:
                ids.add(id)
                yield candidate

    candidates: List[Candidate]

    logger.debug("candidates: Iterating members")
    candidates = list(itertools.islice(
        unique_candidates(heapq.merge(id_iter(), username_iter(), displayname_iter(), nickname_iter())),
        limit))

    if len(candidates) < limit or candidates[-1].rank[0] >= MatchType.EXACT_RECENT_USER:
        logger.debug("candidates: Iterating recent users")
        candidates = list(itertools.islice(
            unique_candidates(heapq.merge(candidates,
                recent_iter(await match_recents(session, input, NickOrUser.USER, False)))),
            limit))

    if len(candidates) < limit or candidates[-1].rank[0] >= MatchType.EXACT_RECENT_NICK:
        logger.debug("candidates: Iterating recent nicks")
        candidates = list(itertools.islice(
            unique_candidates(heapq.merge(candidates,
                recent_iter(await match_recents(session, input, NickOrUser.NICK, False)))),
            limit))

    if len(candidates) < limit or candidates[-1].rank[0] >= MatchType.INFIX_RECENT:
        logger.debug("candidates: Iterating recent infix")
        candidates = list(itertools.islice(
            unique_candidates(heapq.merge(candidates,
                recent_iter(await match_recents(session, input, NickOrUser.USER, True)),
                recent_iter(await match_recents(session, input, NickOrUser.NICK, True)))),
            limit))

    logger.debug("candidates: Done")
    return candidates

async def match_recents(session: AsyncSession, text: str, nu: NickOrUser, infix: bool) -> Sequence[Recent]:
    if nu == NickOrUser.NICK:
        idcol = plugins.log.SavedNick.id
        matchcol = func.lower(plugins.log.SavedNick.nick)
    else:
        idcol = plugins.log.SavedUser.id
        matchcol = func.lower(plugins.log.SavedUser.username + "#" + plugins.log.SavedUser.discrim)
    if infix:
        matchcond = func.strpos(matchcol, text.lower()) > 0
    else:
        matchcond = func.substring(matchcol, 1, len(text)) == text.lower()

    stmt = select(idcol, matchcol).where(matchcond)
    results = []
    for id, match in await session.execute(stmt):
        results.append((id, match, nu, infix))
    return results

@command("whois")
@default_permissions()
@guild_only()
async def whois_command(interaction: Interaction, user: str) -> None:
    assert (guild := interaction.guild) is not None
    await interaction.response.defer(ephemeral=True)

    async with plugins.log.sessionmaker() as session:
        candidates = await select_candidates(1, user, guild, session)

    if not candidates:
        try:
            id = int(user)
        except ValueError:
            await interaction.followup.send("No matches.", ephemeral=True)
            return
    else:
        id = match_id(candidates[0].match)

    content = format("{!m}", id)
    embed = Embed()
    embed.add_field(name="ID", value=format("{!i}", id))
    if not (m := guild.get_member(id)):
        try:
            m = await client.fetch_user(id)
        except discord.HTTPException as e:
            embed.description = "Profile returned {}".format(e.status)
    if m:
        embed.add_field(name="Username", value=format("{!i}#{!i}", m.name, m.discriminator))
        if isinstance(m, Member):
            embed.add_field(name="Nickname", value=format("{!i}", m.nick) if m.nick is not None else "none")
            embed.add_field(name="Roles", inline=False,
                value=", ".join(format("{!M}", role) for role in m.roles if not role.is_default()) or "none")
        else:
            embed.add_field(name="Not on server", value="\u200B")
        if isinstance(m, Member):
            if m.joined_at is not None:
                joined_at = int(m.joined_at.timestamp())
                embed.add_field(name="Joined", value="<t:{}:f>, <t:{}:R>".format(joined_at, joined_at))
        created_at = int(m.created_at.timestamp())
        embed.add_field(name="Created", value="<t:{}:f>, <t:{}:R>".format(created_at, created_at))
        embed.set_thumbnail(url=m.display_avatar.url)

    await interaction.followup.send(content, embed=embed, ephemeral=True)

    async with plugins.tickets.sessionmaker() as session:
        tickets = await plugins.tickets.visible_tickets(session, id)

    async with plugins.log.sessionmaker() as session:
        stmt = (select(plugins.log.SavedMessage)
            .where(plugins.log.SavedMessage.author_id == id)
            .order_by(plugins.log.SavedMessage.id.desc())
            .limit(15))
        msgs = reversed(list((await session.execute(stmt)).scalars()))
        stmt = select(plugins.log.SavedUser).where(plugins.log.SavedUser.id == id)
        users = list((await session.execute(stmt)).scalars())
        stmt = select(plugins.log.SavedNick.nick).where(
            plugins.log.SavedNick.id == id, plugins.log.SavedNick.nick != None)
        nicks = list((await session.execute(stmt)).scalars())

    def item_gen() -> Iterator[PlainItem]:
        first = True
        for ticket in tickets:
            if first:
                yield PlainItem("**Outstanding tickets**\n")
            else:
                yield PlainItem(", ")
            first = False
            yield PlainItem(format("[#{}]({}): {} ({})", ticket.id, ticket.jump_link,
                ticket.describe(target=False, mod=False, dm=False), ticket.status_line))
        first = True
        for msg in msgs:
            if first:
                yield PlainItem("\n\n**Recent messages**\n")
            else:
                yield PlainItem("\n")
            first = False
            created_at = int(snowflake_time(msg.id).timestamp())
            content = msg.content.decode("utf8")
            link = client.get_partial_messageable(msg.channel_id).get_partial_message(msg.id).jump_url
            yield PlainItem(format("{!c} <t:{}:R> [{!i}{}]({})", msg.channel_id, created_at, content[:100],
                "..." if len(content) > 100 else "", link))
        first = True
        seen = set()
        if m:
            seen.add((m.name, m.discriminator))
        for user in users:
            if (user.username, user.discrim) not in seen:
                seen.add((user.username, user.discrim))
                if first:
                    yield PlainItem("\n\n**Past usernames**\n")
                else:
                    yield PlainItem(", ")
                first = False
                yield PlainItem(format("{!i}#{!i}", user.username, user.discrim))
        first = True
        seen = set()
        if isinstance(m, Member) and m.nick is not None:
            seen.add(m.nick)
        for nick in nicks:
            if not nick in seen:
                seen.add(nick)
                if first:
                    yield PlainItem("\n\n**Past nicknames**\n")
                else:
                    yield PlainItem(", ")
                first = False
                yield PlainItem(format("{!i}", nick))

    for content, _ in chunk_messages(item_gen()):
        await interaction.followup.send(content, suppress_embeds=True, ephemeral=True)

def format_server_status(server_status: ServerStatus) -> str:
    if server_status == 1:
        return "not on server"
    else:
        return "{} roles".format(-server_status)

def format_match(rank: MatchRank, match: Union[Member, Recent], guild: Guild) -> str:
    if rank[0] == MatchType.EXACT_ID:
        mtype = "=#"
    elif rank[0] == MatchType.EXACT_USER:
        mtype = "=U"
    elif rank[0] == MatchType.EXACT_NICK:
        mtype = "=N"
    elif rank[0] == MatchType.PREFIX:
        mtype = "\u2192U" if rank[2] == NickOrUser.USER else "\u2192N"
    elif rank[0] == MatchType.INFIX:
        mtype = "\u27F7U" if rank[2] == NickOrUser.USER else "\u27F7N"
    elif rank[0] == MatchType.EXACT_RECENT_USER:
        mtype = "=u"
    elif rank[0] == MatchType.EXACT_RECENT_NICK:
        mtype = "=n"
    elif rank[0] == MatchType.PREFIX_RECENT:
        mtype = "\u2192u" if rank[2] == NickOrUser.USER else "\u2192n"
    elif rank[0] == MatchType.INFIX_RECENT:
        mtype = "\u27F7u" if rank[2] == NickOrUser.USER else "\u27F7n"
    else:
        mtype = "\u2192#"
    if rank[0] == MatchType.EXACT_ID:
        server_status = None
    elif rank[0] == MatchType.PREFIX_ID:
        server_status = rank[-2]
    else:
        server_status = rank[-1]
    if isinstance(match, Member):
        if server_status is None:
            server_status = rank_server_status(match)
        sstat = format_server_status(server_status)
        if match.nick is not None:
            return "{} \uFF5C {}#{} \uFF5C {} \uFF5C {} \uFF5C ({}) [{}]".format(
                match.id, match.name, match.discriminator, match.display_name, match.nick, sstat, mtype)
        else:
            return "{} \uFF5C {}#{} \uFF5C {} \uFF5C ({}) [{}]".format(
                match.id, match.name, match.discriminator, match.display_name, sstat, mtype)
    else:
        id, aka, _, _ = match
        if (m := guild.get_member(id)) is not None:
            if server_status is None:
                server_status = rank_server_status(m)
            sstat = format_server_status(server_status)
            if m.nick is not None:
                return "{} \uFF5C {}#{} \uFF5C {} \uFF5C aka: {} \uFF5C ({}) [{}]".format(
                    id, m.name, m.discriminator, m.nick, aka, sstat, mtype)
            else:
                return "{} \uFF5C {}#{} \uFF5C aka: {} \uFF5C ({}) [{}]".format(
                    id, m.name, m.discriminator, aka, sstat, mtype)
        else:
            if server_status is None:
                server_status = rank_server_status(None)
            sstat = format_server_status(server_status)
            return "{} ??? \uFF5C aka: {} \uFF5C ({}) [{}]".format(id, aka, sstat, mtype)

@whois_command.autocomplete("user")
async def whois_autocomplete(interaction: Interaction, input: str) -> List[Choice[str]]:
    assert (guild := interaction.guild) is not None
    logger.debug("Start autocomplete")
    if not input:
        results = []
    else:
        async with plugins.log.sessionmaker() as session:
            results = [Choice(name=format_match(c.rank, c.match, guild), value=str(match_id(c.match)))
                for c in await select_candidates(25, input, guild, session)]
    logger.debug("End autocomplete")
    return results

filling_event: threading.Event = threading.Event()

@cog
class Whois(Cog):
    """Maintain username cache"""
    async def cog_load(self) -> None:
        await self.on_ready()

    @Cog.listener()
    async def on_ready(self) -> None:
        global id_trie, username_trie, displayname_trie, nickname_trie, filling_event
        filling_event.set()
        filling_event = threading.Event()

        id_trie = IdTrie()
        username_trie = InfixTrie()
        displayname_trie = InfixTrie()
        nickname_trie = InfixTrie()

        def fill_trie(event: threading.Event, id_trie: IdTrie, username_trie: InfixTrie, displayname_trie: InfixTrie, nickname_trie: InfixTrie, members: List[Member]) -> None:
            logger.debug("Starting to fill tries")
            i = 0
            for member in members:
                if event.is_set():
                    break
                id_trie.insert(member.id)
                username_trie.insert(member.name + "#" + member.discriminator, member.id)
                if member.global_name is not None:
                    displayname_trie.insert(member.global_name, member.id)
                if member.nick is not None:
                    nickname_trie.insert(member.nick, member.id)

                i += 1
                if i % 10000 == 0:
                    logger.debug("Filling tries: {}".format(i))
            logger.debug("Done filling tries")

        asyncio.get_event_loop().run_in_executor(None,
            fill_trie, filling_event, id_trie, username_trie, displayname_trie, nickname_trie,
            list(client.get_all_members()))

    @Cog.listener()
    async def on_member_join(self, member: Member) -> None:
        id_trie.insert(member.id)
        username_trie.insert(member.name + "#" + member.discriminator, member.id)
        if member.global_name is not None:
            displayname_trie.insert(member.global_name, member.id)
        if member.nick is not None:
            nickname_trie.insert(member.nick, member.id)

    @Cog.listener()
    async def on_raw_member_remove(self, payload: RawMemberRemoveEvent) -> None:
        id_trie.insert(payload.user.id)
        username_trie.delete(payload.user.name + "#" + payload.user.discriminator, payload.user.id)
        if payload.user.global_name is not None:
            displayname_trie.delete(payload.user.global_name, payload.user.id)
        if isinstance(payload.user, Member) and payload.user.nick is not None:
            nickname_trie.delete(payload.user.nick, payload.user.id)

    @Cog.listener()
    async def on_member_update(self, before: Member, after: Member) -> None:
        if before.nick != after.nick:
            if before.nick is not None:
                nickname_trie.delete(before.nick, before.id)
            if after.nick is not None:
                nickname_trie.insert(after.nick, after.id)

    @Cog.listener()
    async def on_user_update(self, before: User, after: User) -> None:
        if before.name != after.name or before.discriminator != after.discriminator:
            username_trie.delete(before.name + "#" + before.discriminator, before.id)
            username_trie.insert(after.name + "#" + after.discriminator, after.id)
        if before.display_name != after.display_name:
            displayname_trie.delete(before.display_name, before.id)
            displayname_trie.insert(after.display_name, after.id)
