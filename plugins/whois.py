from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
import enum
from functools import total_ordering
from heapq import heappop, heappush, heappushpop
import logging
import re
import threading
from typing import Awaitable, Callable, Collection, Dict, Iterator, List, Literal, Optional, Sequence, Set, Tuple, Union

import datrie
import discord
from discord import Embed, Interaction, Member, RawMemberRemoveEvent, User
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
class MatchType(enum.Enum):
    EXACT_ID = 9
    EXACT_USER = 8
    EXACT_NICK = 7
    PREFIX = 6
    INFIX = 5
    EXACT_RECENT_USER = 4
    EXACT_RECENT_NICK = 3
    PREFIX_RECENT = 2
    INFIX_RECENT = 1
    PREFIX_ID = 0

    def __lt__(self, other: MatchType) -> bool:
        return self.value < other.value

@total_ordering
class NickOrUser(enum.Enum):
    NICK = 0
    USER = 1

    def __lt__(self, other: MatchType) -> bool:
        return self.value < other.value

ServerStatus = int
MatchRank = Union[
    Tuple[Literal[MatchType.EXACT_ID],],
    Tuple[Literal[MatchType.EXACT_USER, MatchType.EXACT_NICK], ServerStatus],
    Tuple[Literal[MatchType.PREFIX, MatchType.INFIX], int, NickOrUser, ServerStatus],
    Tuple[Literal[MatchType.EXACT_RECENT_USER, MatchType.EXACT_RECENT_NICK], ServerStatus],
    Tuple[Literal[MatchType.PREFIX_RECENT, MatchType.INFIX_RECENT], int, NickOrUser, ServerStatus],
    Tuple[Literal[MatchType.PREFIX_ID], ServerStatus, int]]

Recent = Tuple[int, str, NickOrUser, bool]

def rank_server_status(m: Optional[Member]) -> ServerStatus:
    return len(m.roles) if m else -1

class InfixTrie:
    # Most common characters to appear in nicknames and usernames
    common_chars = (" !\"$&'()*+,-./0123456789;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~\xa3"
    "\xae\xb0\xb2\xc6\xd8\xe0\xe1\xe3\xe4\xe5\xe6\xe7\xe9\xea\xeb\xed\xef\xf1\xf3\xf6\xf8\xfc\u0110\u0130\u0131\u015f"
    "\u026a\u0280\u02de\u0334\u0336\u0337\u035c\u0361\u0394\u03a3\u03b1\u03b5\u03b9\u03bb\u03bd\u03bf\u03c0\u03c1\u03c2"
    "\u03c3\u03c4\u0410\u0430\u0431\u0432\u0433\u0434\u0435\u0436\u0438\u0439\u043a\u043b\u043c\u043d\u043e\u0440\u0441"
    "\u0442\u0443\u0445\u0447\u044c\u044f\u0627\u0628\u062d\u062f\u0631\u0632\u0639\u0644\u0645\u0646\u0647\u0648\u064a"
    "\u17b5\u1cbc\u1d00\u1d07\u1d0f\u1d1b\u1d1c\u1d43\u1d49\u2019\u2020\u2022\u2122\u2605\u2606\u2661\u2665\u2727\u2728"
    "\u2764\u300e\u300f\u30a4\u30b8\u30b9\u30c4\u30c8\u30e9\u30ea\u30eb\u30f3\u30fb\u30fc\u4e00\u4eba\u5927\u7684\ua9c1"
    "\ua9c2\uc774\U0001d404\U0001d41a\U0001d422\U0001d427\U0001d42b\U0001d452\U0001d4b6\U0001d4be\U0001d4c3\U0001d4ea"
    "\U0001d4ee\U0001d4f1\U0001d4f2\U0001d4f5\U0001d4f7\U0001d4f8\U0001d4fb\U0001d556\U0001d586\U0001d588\U0001d58a"
    "\U0001d58c\U0001d58d\U0001d58e\U0001d591\U0001d592\U0001d593\U0001d594\U0001d597\U0001d598\U0001d599\U0001d59a"
    "\U0001f338\U0001f451\U0001f525\U0001f5a4\U0001f608\U0001f940\U0001f98b")
    assert len(common_chars) == 254
    uncommon_re = re.compile("[^" + re.escape(common_chars) + "]")

    trie: datrie.Trie
    uncommon: Dict[str, Set[int]]
    lock: threading.Lock

    def __init__(self):
        self.trie = datrie.Trie("\x00" + self.common_chars)
        self.uncommon = defaultdict(set)
        self.lock = threading.Lock()

    def common_key_iter(self, key: str):
        common_key = re.sub(self.uncommon_re, "\x00", key)
        for i in range(len(common_key)):
            if common_key[i] != "\x00":
                yield common_key[i:]


    def insert(self, key: str, value: int) -> None:
        with self.lock:
            for ch in re.findall(self.uncommon_re, key):
                self.uncommon[ch].add(value)
            for trie_key in self.common_key_iter(key):
                if (s := self.trie.get(trie_key)) is not None:
                    s.add(value)
                else:
                    self.trie[trie_key] = {value}

    def delete(self, key: str, value: int) -> None:
        with self.lock:
            for ch in re.findall(self.uncommon_re, key):
                self.uncommon[ch].remove(value)
            for trie_key in self.common_key_iter(key):
                if (s := self.trie.get(trie_key)) is not None:
                    s.remove(value)

    def lookup(self, key: str) -> Set[int]:
        """Returns ids that *might* match the key, but not necessarily do."""
        results: Set[int] = set()
        with self.lock:
            for ch in re.findall(self.uncommon_re, key):
                if (s := self.uncommon.get(ch)) is not None:
                    results |= s
            common_key = re.sub(self.uncommon_re, "\x00", key)
            for s in self.trie.values(common_key):
                results |= s
        return results

username_trie: InfixTrie = InfixTrie()
displayname_trie: InfixTrie = InfixTrie()
nickname_trie: InfixTrie = InfixTrie()

def rank_member_match(text: str, m: Member) -> Optional[MatchRank]:
    text_l = text.lower()
    user_l = m.name.lower() + "#" + m.discriminator
    nick_l = m.nick.lower() if m.nick is not None else None
    server_status = rank_server_status(m)
    if text_l == user_l:
        return MatchType.EXACT_USER, server_status
    if text_l == nick_l:
        return MatchType.EXACT_NICK, server_status
    if user_l.startswith(text_l):
        return MatchType.PREFIX, len(text_l) - len(user_l), NickOrUser.USER, server_status
    if nick_l is not None and nick_l.startswith(text_l):
        return MatchType.PREFIX, len(text_l) - len(nick_l), NickOrUser.NICK, server_status
    if text_l in user_l:
        return MatchType.INFIX, len(text_l) - len(user_l), NickOrUser.USER, server_status
    if nick_l is not None and text_l in nick_l:
        return MatchType.INFIX, len(text_l) - len(nick_l), NickOrUser.NICK, server_status
    if str(m.id).startswith(text):
        return MatchType.PREFIX_ID, server_status, -m.id

def rank_recent_match(text: str, recent: Recent, server_status: ServerStatus) -> MatchRank:
    _, match, nu, infix = recent
    if text == match:
        if nu == NickOrUser.USER:
            return MatchType.EXACT_RECENT_USER, server_status
        else:
            return MatchType.EXACT_RECENT_NICK, server_status
    if infix:
        return MatchType.INFIX_RECENT, len(text) - len(match), nu, server_status
    else:
        return MatchType.PREFIX_RECENT, len(text) - len(match), nu, server_status

def match_id(match: Union[Member, Recent]):
    return match.id if isinstance(match, Member) else match[0]

@dataclass(order=True)
class Candidate:
    rank: MatchRank
    match: Union[Member, Recent] = field(compare=False)

async def select_candidates(limit: int, text: str, id_lookup: Callable[[int], Optional[Member]],
    member_source: Callable[[], Collection[int]],
    recent_source: Callable[[str, NickOrUser, bool], Awaitable[Sequence[Recent]]]) -> Sequence[Candidate]:

    candidates: List[Candidate] = []
    ids: Set[int] = set()
    def heapfill(rank: MatchRank, match: Union[Member, Recent]) -> None:
        id = match_id(match)
        if id not in ids:
            ids.add(id)
            if len(candidates) >= limit:
                ids.remove(match_id(heappushpop(candidates, Candidate(rank, match)).match))
            else:
                heappush(candidates, Candidate(rank, match))

    try:
        int_text = int(text)
    except ValueError:
        pass
    else:
        if (m := id_lookup(int_text)):
            heapfill((MatchType.EXACT_ID,), m)

    if len(candidates) < limit:
        logger.debug("candidates: Iterating members")
        for id in member_source():
            if (m := id_lookup(id)) is not None and (rank := rank_member_match(text, m)) is not None:
                heapfill(rank, m)

    if len(candidates) < limit or candidates[0].rank[0] <= MatchType.EXACT_RECENT_USER:
        logger.debug("candidates: Iterating recent users")
        for recent in await recent_source(text, NickOrUser.USER, False):
            server_status = rank_server_status(id_lookup(recent[0]))
            rank = rank_recent_match(text, recent, server_status)
            heapfill(rank, recent)

    if len(candidates) < limit or candidates[0].rank[0] <= MatchType.EXACT_RECENT_NICK:
        logger.debug("candidates: Iterating recent nicks")
        for recent in await recent_source(text, NickOrUser.NICK, False):
            server_status = rank_server_status(id_lookup(recent[0]))
            rank = rank_recent_match(text, recent, server_status)
            heapfill(rank, recent)

    if len(candidates) < limit or candidates[0].rank[0] <= MatchType.INFIX_RECENT:
        logger.debug("candidates: Iterating recent users (infix)")
        for recent in await recent_source(text, NickOrUser.USER, True):
            server_status = rank_server_status(id_lookup(recent[0]))
            rank = rank_recent_match(text, recent, server_status)
            heapfill(rank, recent)
        logger.debug("candidates: Iterating recent nicks (infix)")
        for recent in await recent_source(text, NickOrUser.NICK, True):
            server_status = rank_server_status(id_lookup(recent[0]))
            rank = rank_recent_match(text, recent, server_status)
            heapfill(rank, recent)

    logger.debug("candidates: Done")
    return [heappop(candidates) for _ in range(min(limit, len(candidates)))]

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
        lookup_id = guild.get_member
        member_source = lambda: username_trie.lookup(user) | displayname_trie.lookup(user) | nickname_trie.lookup(user)
        recent_source: Callable[[str, NickOrUser, bool], Awaitable[Sequence[Recent]]]
        recent_source = lambda text, nu, infix: match_recents(session, text, nu, infix)

        candidates = await select_candidates(1, user, lookup_id, member_source, recent_source)

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
    if server_status == -1:
        return "not on server"
    else:
        return "{} roles".format(server_status)

def format_match(rank: MatchRank, match: Union[Member, Recent], lookup_id: Callable[[int], Optional[Member]]) -> str:
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
            return "{} \uFF5C {}#{} \uFF5C {} \uFF5C ({}) [{}]".format(
                match.id, match.name, match.discriminator, match.nick, sstat, mtype)
        else:
            return "{} \uFF5C {}#{} \uFF5C ({}) [{}]".format(
                match.id, match.name, match.discriminator, sstat, mtype)
    else:
        id, aka, _, _ = match
        if (m := lookup_id(id)):
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
    if not input: return []
    async with plugins.log.sessionmaker() as session:
        lookup_id = guild.get_member
        member_source = lambda: username_trie.lookup(input) | displayname_trie.lookup(input) | nickname_trie.lookup(input)
        recent_source: Callable[[str, NickOrUser, bool], Awaitable[Sequence[Recent]]]
        recent_source = lambda text, nu, infix: match_recents(session, text, nu, infix)

        results = [Choice(name=format_match(c.rank, c.match, lookup_id), value=str(match_id(c.match)))
            for c in reversed(await select_candidates(25, input, lookup_id, member_source, recent_source))]
    logger.debug("End autocomplete")
    return results

@cog
class Whois(Cog):
    """Maintain username cache"""
    async def cog_load(self) -> None:
        await self.on_ready()

    @Cog.listener()
    async def on_ready(self) -> None:
        global username_trie, displayname_trie, nickname_trie
        username_trie = InfixTrie()
        displayname_trie = InfixTrie()
        nickname_trie = InfixTrie()

        def fill_trie(members: List[Member]) -> None:
            for member in members:
                username_trie.insert(member.name + "#" + member.discriminator, member.id)
                if member.global_name is not None:
                    displayname_trie.insert(member.global_name, member.id)
                if member.nick is not None:
                    nickname_trie.insert(member.nick, member.id)

        await asyncio.get_event_loop().run_in_executor(None, fill_trie, list(client.get_all_members()))

    @Cog.listener()
    async def on_member_join(self, member: Member) -> None:
        username_trie.insert(member.name + "#" + member.discriminator, member.id)
        if member.global_name is not None:
            displayname_trie.insert(member.global_name, member.id)
        if member.nick is not None:
            nickname_trie.insert(member.nick, member.id)

    @Cog.listener()
    async def on_raw_member_remove(self, payload: RawMemberRemoveEvent) -> None:
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
