import asyncio
from datetime import datetime, timedelta
import enum
import logging
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, List, Optional, Sequence, Union, cast

import discord
from discord import AllowedMentions, ButtonStyle, Interaction, InteractionType, PartialMessage, TextStyle, Thread
from discord.abc import Messageable
if TYPE_CHECKING:
    import discord.types.interactions
from discord.ui import Button, Modal, TextInput, View
import sqlalchemy
from sqlalchemy import BOOLEAN, TEXT, TIMESTAMP, BigInteger, ForeignKey, Integer, nulls_first, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.client import client
from bot.cogs import Cog, cog, command
from bot.commands import Context, cleanup
from bot.locations import location
from bot.privileges import priv
import plugins
import util.db
from util.discord import DurationConverter, UserError, format

logger = logging.getLogger(__name__)

user_mentions = AllowedMentions(everyone=False, roles=False, users=True)

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True, expire_on_commit=False)

@registry.mapped
class Poll:
    __tablename__ = "polls"
    __table_args__ = {"schema": "consensus"}

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    votes_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comment: Mapped[int] = mapped_column(TEXT, nullable=False)
    duration: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timeout: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    timeout_notified: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, message_id: int, votes_id: int, guild_id: int, channel_id: int, thread_id: Optional[int],
            author_id: int, comment: str, duration: int, timeout: datetime, timeout_notified: bool) -> None:
            ...

    async def get_votes_message(self) -> Optional[PartialMessage]:
        channel_id = self.channel_id if self.thread_id is None else self.thread_id
        try:
            if not isinstance(channel := await client.fetch_channel(channel_id), Messageable):
                return None
        except (discord.NotFound, discord.Forbidden):
            return None
        return channel.get_partial_message(self.votes_id)

@registry.mapped
class Concern:
    __tablename__ = "concerns"
    __table_args__ = {"schema": "consensus"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(Poll.message_id, ondelete="CASCADE"), nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comment: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, poll_id: int, author_id: int, comment: str) -> None: ...

class VoteType(enum.Enum):
    UPVOTE = "Upvote"
    NEUTRAL = "Neutral"
    DOWNVOTE = "Downvote"

@registry.mapped
class Vote:
    __tablename__ = "votes"
    __table_args__ = {"schema": "consensus"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(Poll.message_id, ondelete="CASCADE"), nullable=False)
    voter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vote: Mapped[VoteType] = mapped_column(sqlalchemy.Enum(VoteType, schema="consensus"), nullable=False)
    after_concern: Mapped[Optional[int]] = mapped_column(BigInteger)
    comment: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, poll_id: int, voter_id: int, vote: VoteType, after_concern: Optional[int], comment: str
            ) -> None: ...

timeout_event = asyncio.Event()

def timeouts_updated() -> None:
    timeout_event.set()

async def handle_timeouts() -> None:
    await client.wait_until_ready()

    while True:
        try:
            async with sessionmaker() as session:
                stmt = select(Poll).where(Poll.timeout_notified == False)
                polls = (await session.execute(stmt)).scalars().all()

                min_timeout = None
                now = datetime.utcnow()
                try:
                    for poll in polls:
                        if poll.timeout <= now:
                            if (msg := await poll.get_votes_message()) is None:
                                await session.delete(poll)
                            else:
                                try:
                                    await msg.channel.send(format("Poll timed out {!m}", poll.author_id),
                                        allowed_mentions=user_mentions, reference=msg)
                                except discord.HTTPException:
                                    pass
                            poll.timeout_notified = True
                        else:
                            if min_timeout is None or poll.timeout < min_timeout:
                                min_timeout = poll.timeout
                finally:
                    await session.commit()

            delay = (min_timeout - datetime.utcnow()).total_seconds() if min_timeout is not None else 86400.0
            logger.debug("Waiting for upcoming timeout in {} seconds".format(delay))
            try:
                await asyncio.wait_for(timeout_event.wait(), timeout=delay)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass
            timeout_event.clear()
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in poll timeout task", exc_info=True)
            await asyncio.sleep(60)

timeout_task: asyncio.Task[None]

@plugins.init
async def init() -> None:
    global timeout_task
    await util.db.init(util.db.get_ddl(
        CreateSchema("consensus"),
        registry.metadata.create_all))
    timeout_task = asyncio.create_task(handle_timeouts())
    plugins.finalizer(timeout_task.cancel)

class PollView(View):
    def __init__(self, poll_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(Button(style=ButtonStyle.primary, label="Vote",
            custom_id="{}:{}:Vote".format(__name__, poll_id)))
        self.add_item(Button(style=ButtonStyle.primary, label="Raise concern",
            custom_id="{}:{}:RaiseConcern".format(__name__, poll_id)))
        self.add_item(Button(style=ButtonStyle.primary, label="Retract concern",
            custom_id="{}:{}:RetractConcern".format(__name__, poll_id)))
        self.add_item(Button(style=ButtonStyle.danger, label="Close",
            custom_id="{}:{}:Close".format(__name__, poll_id)))

def merge_vote_concern(votes: Iterable[Vote], concerns: Iterable[Concern]) -> Iterator[Union[Vote, Concern]]:
    it_votes = iter(votes)
    it_concerns = iter(concerns)
    try:
        vote = next(it_votes)
    except StopIteration:
        yield from it_concerns
        return
    try:
        concern = next(it_concerns)
    except StopIteration:
        yield vote
        yield from it_votes
        return
    while True:
        if vote.after_concern is None or concern.id > vote.after_concern:
            yield vote
            try:
                vote = next(it_votes)
            except StopIteration:
                yield concern
                yield from it_concerns
                return
        else:
            yield concern
            try:
                concern = next(it_concerns)
            except StopIteration:
                yield vote
                yield from it_votes
                return

def render_poll(votes: Sequence[Vote], concerns: Sequence[Concern]) -> str:
    rows = ["Votes and concerns:"]
    for item in merge_vote_concern(votes, concerns):
        if isinstance(item, Vote):
            if item.vote == VoteType.UPVOTE:
                emoji = "\u2705"
            elif item.vote == VoteType.NEUTRAL:
                emoji = "\U0001F518"
            else:
                emoji = "\u274C"
            row = format("{!m}: {}", item.voter_id, emoji)
            if item.comment:
                row += " " + item.comment
        else:
            row = format("\u26A0 {!m}: {}", item.author_id, item.comment)
        rows.append(row)
    return "\n".join(rows)

async def edit_poll(poll_id: Optional[int], msg: PartialMessage, votes: Sequence[Vote],
    concerns: Sequence[Concern]) -> None:
    await msg.edit(content=render_poll(votes, concerns)[:4000], view=PollView(poll_id) if poll_id is not None else None,
        allowed_mentions=user_mentions)

async def sync_poll(session: AsyncSession, poll_id: int, msg: PartialMessage):
    stmt = select(Vote).where(Vote.poll_id == poll_id).order_by(nulls_first(Vote.after_concern), Vote.id)
    votes = (await session.execute(stmt)).scalars().all()
    stmt = select(Concern).where(Concern.poll_id == poll_id).order_by(Concern.id)
    concerns = (await session.execute(stmt)).scalars().all()
    await edit_poll(poll_id, msg, votes, concerns)


async def cast_vote(interaction: Interaction, poll_id: int, vote_type: Optional[VoteType], comment: str,
    after_concern: Optional[int]) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        if (msg := await poll.get_votes_message()) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        stmt = select(Vote).where(Vote.poll_id == poll_id, Vote.voter_id == interaction.user.id).limit(1)
        vote = (await session.execute(stmt)).scalar()
        if vote is not None:
            await session.delete(vote)
        if vote_type is not None:
            session.add(Vote(poll_id=poll_id, voter_id=interaction.user.id, vote=vote_type, after_concern=after_concern,
                comment=comment))
        await session.commit()
        await sync_poll(session, poll_id, msg)
        if vote is None:
            if vote_type is None:
                text = "Nothing changed."
            else:
                text = "Vote added."
        else:
            if vote_type is None:
                text = "Vote retracted."
            else:
                text = "Vote updated."
        await interaction.response.send_message(text, ephemeral=True)

async def raise_concern(interaction: Interaction, poll_id: int, comment: str) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        if (msg := await poll.get_votes_message()) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        session.add(Concern(poll_id=poll_id, author_id=interaction.user.id, comment=comment))
        poll.timeout = datetime.utcnow() + timedelta(seconds=poll.duration)
        poll.timeout_notified = False
        await session.commit()
        await sync_poll(session, poll_id, msg)
        stmt = select(Vote).where(Vote.poll_id == poll_id).order_by(Vote.id)
        votes = (await session.execute(stmt)).scalars().all()
        if len(votes):
            await msg.channel.send(" ".join(format("{!m}", vote.voter_id) for vote in votes),
                allowed_mentions=user_mentions, reference=msg)
        await interaction.response.send_message("Concern added.", ephemeral=True)
        timeouts_updated()

async def retract_concern(interaction: Interaction, poll_id: int, id: int) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        if (msg := await poll.get_votes_message()) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        if (concern := await session.get(Concern, id)) is None:
            await interaction.response.send_message("Invalid concern.", ephemeral=True)
            return
        await session.delete(concern)
        await session.commit()
        await sync_poll(session, poll_id, msg)
        await interaction.response.send_message("Concern retracted.", ephemeral=True)

class VoteModal(Modal):
    def __init__(self, poll_id: int, concerns: List[str], vote: Optional[VoteType], comment: str,
        after_concern: Optional[int]) -> None:
        self.poll_id = poll_id
        self.after_concern = after_concern
        super().__init__(title="Vote", timeout=600)
        if concerns:
            self.add_item(TextInput(style=TextStyle.paragraph, required=False,
                label="Concerns raised since your last vote", default="\n\n".join(concerns)[:4000]))

        # Selects not allowed in modals for now
        #self.vote = Select(placeholder="Vote", min_values=1, max_values=1, options=[
        #    SelectOption(label="Upvote", emoji="\u2705", default=vote==VoteType.UPVOTE),
        #    SelectOption(label="Neutral", emoji="\U0001F518", default=vote==VoteType.NEUTRAL),
        #    SelectOption(label="Downvote", emoji="\u274C", default=vote==VoteType.DOWNVOTE),
        #    SelectOption(label="None (retract vote)")])
        self.vote = TextInput(required=False, max_length=1,
            label="[Y]es\u2705 / [N]o\u274C / [A]bstain \U0001F518 / [R]etract \u21A9")
        if vote == VoteType.UPVOTE:
            self.vote.default = "Y"
        elif vote == VoteType.NEUTRAL:
            self.vote.default = "A"
        elif vote == VoteType.DOWNVOTE:
            self.vote.default = "N"
        self.add_item(self.vote)

        self.comment = TextInput(style=TextStyle.paragraph, required=False, max_length=300,
            label="Comment (optional)", default=comment)
        self.add_item(self.comment)

    async def on_submit(self, interaction: Interaction) -> None:
        #if self.vote.values == ["Upvote"]:
        #    vote = VoteType.UPVOTE
        #elif self.vote.values == ["Neutral"]:
        #    vote = VoteType.NEUTRAL
        #elif self.vote.values == ["Downvote"]:
        #    vote = VoteType.DOWNVOTE
        #else:
        #    vote = None
        vote_char = str(self.vote)[:1].upper()
        if vote_char == "Y":
            vote = VoteType.UPVOTE
        elif vote_char == "A":
            vote = VoteType.NEUTRAL
        elif vote_char == "N":
            vote = VoteType.DOWNVOTE
        else:
            vote = None
        await cast_vote(interaction, self.poll_id, vote, comment=str(self.comment)[:300],
            after_concern=self.after_concern)

class RaiseConcernModal(Modal):
    concern = TextInput(style=TextStyle.paragraph, required=True, max_length=300, label="Concern")

    def __init__(self, poll_id: int) -> None:
        self.poll_id = poll_id
        super().__init__(title="Raise concern", timeout=600)

    async def on_submit(self, interaction: Interaction) -> None:
        if str(self.concern):
            await raise_concern(interaction, self.poll_id, str(self.concern)[:300])

class RetractConcernModal(Modal):
    def __init__(self, poll_id: int, concerns: Dict[int, str]) -> None:
        self.poll_id = poll_id
        super().__init__(title="Raise concern", timeout=600)
        # Selects not available in modals for now:
        # self.concern = Select(placeholder="Select the concern to retract", min_values=1, max_values=1,
        #    options=[SelectOption(label=concern[:300], value=str(key)) for key, concern in concerns.items()])
        self.keys = list(concerns)
        self.concern = TextInput(required=True,
            label="Which concern to retract? {}...{}".format(1, len(concerns)))
        self.add_item(self.concern)

    async def on_submit(self, interaction: Interaction) -> None:
        #if self.concern.values:
        #    try:
        #        key = int(self.concern.values[0])
        #    except ValueError:
        #        return
        try:
            index = int(str(self.concern))
            if index <= 0 or index > len(self.keys):
                raise IndexError()
            key = self.keys[index - 1]
        except (ValueError, IndexError):
            return
        await retract_concern(interaction, self.poll_id, key)

async def prompt_vote(interaction: Interaction, poll_id: int) -> None:
    async with sessionmaker() as session:
        stmt = select(Vote).where(Vote.poll_id == poll_id, Vote.voter_id == interaction.user.id).limit(1)
        vote = (await session.execute(stmt)).scalar()
        stmt = select(Concern).where(Concern.poll_id == poll_id)
        concerns = (await session.execute(stmt)).scalars().all()
        await interaction.response.send_modal(VoteModal(poll_id, vote=None if vote is None else vote.vote,
            concerns=[concern.comment for concern in concerns
                if vote is not None and (vote.after_concern is None or concern.id > vote.after_concern)],
            comment="" if vote is None else vote.comment, after_concern=concerns[-1].id if concerns else None))

async def prompt_raise_concern(interaction: Interaction, poll_id: int) -> None:
    await interaction.response.send_modal(RaiseConcernModal(poll_id))

async def prompt_retract_concern(interaction: Interaction, poll_id: int) -> None:
    async with sessionmaker() as session:
        stmt = select(Concern).where(Concern.poll_id == poll_id, Concern.author_id == interaction.user.id)
        concerns = {}
        for concern in (await session.execute(stmt)).scalars():
            concerns[concern.id] = concern.comment
        if concerns:
            await interaction.response.send_modal(RetractConcernModal(poll_id, concerns))

async def close_poll(interaction: Interaction, poll_id: int) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            return
        if poll.author_id != interaction.user.id:
            return
        stmt = select(Vote).where(Vote.poll_id == poll_id).order_by(nulls_first(Vote.after_concern), Vote.id)
        votes = (await session.execute(stmt)).scalars().all()
        stmt = select(Concern).where(Concern.poll_id == poll_id).order_by(Concern.id)
        concerns = (await session.execute(stmt)).scalars().all()
        await session.delete(poll)
        try:
            if (msg := await poll.get_votes_message()) is not None:
                await edit_poll(None, msg, votes, concerns)
        except discord.HTTPException:
            pass
        await session.commit()


@cog
class ConsensusCog(Cog):
    @Cog.listener()
    async def on_interaction(self, interaction: Interaction) -> None:
        if interaction.type != InteractionType.component or interaction.data is None:
            return
        data = cast("discord.types.interactions.MessageComponentInteractionData", interaction.data)
        if data["component_type"] != 2:
            return
        if ":" not in data["custom_id"]:
            return
        mod, rest = data["custom_id"].split(":", 1)
        if mod != __name__ or ":" not in rest:
            return
        poll_id, action = rest.split(":", 1)
        try:
            poll_id = int(poll_id)
        except ValueError:
            return
        if action == "Vote":
            await prompt_vote(interaction, poll_id)
        elif action == "RaiseConcern":
            await prompt_raise_concern(interaction, poll_id)
        elif action == "RetractConcern":
            await prompt_retract_concern(interaction, poll_id)
        elif action == "Close":
            await close_poll(interaction, poll_id)

    @cleanup
    @command("poll")
    @location("poll")
    async def poll(self, ctx: Context, duration: DurationConverter, *, comment: str) -> None:
        """
        Create a poll with the specified timeout duration and the given message.
        """
        if ctx.guild is None:
            raise UserError("This can only be used in a guild")
        if not comment:
            raise UserError("Poll comment must not be empty")
        async with sessionmaker() as session:
            msg = await ctx.channel.send(format("Poll by {!m}:\n\n{}", ctx.author, comment[:3000]),
                allowed_mentions=AllowedMentions(everyone=False, roles=False, users=[ctx.author]))
            votes = await ctx.channel.send(render_poll([], []), view=PollView(msg.id),
                allowed_mentions=user_mentions)

            if isinstance(ctx.channel, Thread):
                channel_id = ctx.channel.parent_id
                thread_id = ctx.channel.id
            else:
                channel_id = ctx.channel.id
                thread_id = None
            session.add(Poll(message_id=msg.id, votes_id=votes.id, guild_id=ctx.guild.id, channel_id=channel_id,
                thread_id=thread_id, author_id=ctx.author.id, comment=comment, duration=int(duration.total_seconds()),
                timeout=datetime.utcnow() + duration, timeout_notified=False))
            await session.commit()
            timeouts_updated()

    @cleanup
    @command("polls")
    @priv("mod")
    async def polls(self, ctx: Context) -> None:
        async with sessionmaker() as session:
            output = ""
            stmt = select(Poll)
            for poll in (await session.execute(stmt)).scalars():
                channel_id = poll.channel_id if poll.thread_id is None else poll.thread_id
                channel = ctx.bot.get_partial_messageable(channel_id, guild_id=ctx.guild and ctx.guild.id)
                link = channel.get_partial_message(poll.message_id).jump_url + "\n"

                if len(output) + len(link) > 2000:
                    await ctx.send(output)
                    output = link
                else:
                    output += link
            if output:
                await ctx.send(output)
