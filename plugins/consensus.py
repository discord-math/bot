from datetime import datetime, timedelta
import enum
import logging
import re
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, List, Literal, Optional, Pattern, Sequence, Union, cast

import discord
from discord import AllowedMentions, ButtonStyle, Interaction, InteractionType, PartialMessage, TextStyle, Thread
from discord.abc import Messageable
if TYPE_CHECKING:
    import discord.types.interactions
from discord.ui import Button, Modal, TextInput, View
import sqlalchemy
from sqlalchemy import ARRAY, BOOLEAN, TEXT, TIMESTAMP, BigInteger, ForeignKey, Integer, nulls_first, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.acl import privileged
from bot.client import client
from bot.cogs import Cog, cog, command
from bot.commands import Context, cleanup
from bot.tasks import task
import plugins
import util.db
from util.discord import DurationConverter, PlainItem, UserError, chunk_messages, format, retry

logger = logging.getLogger(__name__)

user_mentions = AllowedMentions(everyone=False, roles=False, users=True)

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True, expire_on_commit=False)

class PollType(enum.Enum):
    COUNTED = "Counted"
    CHOICE = "Choice"
    WITH_COMMENTS = "WithComments"
    WITH_CONCERNS = "WithConcerns"

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
    comment: Mapped[str] = mapped_column(TEXT, nullable=False)
    duration: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timeout: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    timeout_notified: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)
    poll: Mapped[PollType] = mapped_column(sqlalchemy.Enum(PollType, schema="consensus"), nullable=False)
    options: Mapped[List[str]] = mapped_column(ARRAY(TEXT), nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, message_id: int, votes_id: int, guild_id: int, channel_id: int, thread_id: Optional[int],
            author_id: int, comment: str, duration: int, timeout: datetime, timeout_notified: bool, poll: PollType,
            options: List[str]) -> None:
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

@registry.mapped
class Vote:
    __tablename__ = "votes"
    __table_args__ = {"schema": "consensus"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(Poll.message_id, ondelete="CASCADE"), nullable=False)
    voter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    choice_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    after_concern: Mapped[Optional[int]] = mapped_column(BigInteger)
    comment: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, poll_id: int, voter_id: int, choice_index: int, after_concern: Optional[int], comment: str
            ) -> None: ...

@task(name="Poll timeout task", every=86400)
async def timeout_task() -> None:
    await client.wait_until_ready()

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
                            reference = msg
                            channel = msg.channel
                            await retry(lambda: channel.send(format("Poll timed out {!m}", poll.author_id),
                                allowed_mentions=user_mentions, reference=reference), attempts=10)
                        except discord.HTTPException:
                            pass
                    poll.timeout_notified = True
                else:
                    if min_timeout is None or poll.timeout < min_timeout:
                        min_timeout = poll.timeout
        finally:
            await session.commit()

    if min_timeout is not None:
        delay = (min_timeout - datetime.utcnow()).total_seconds()
        timeout_task.run_coalesced(delay)
        logger.debug("Waiting for upcoming timeout in {} seconds".format(delay))

@plugins.init
async def init() -> None:
    global timeout_task
    await util.db.init(util.db.get_ddl(
        CreateSchema("consensus"),
        registry.metadata.create_all))
    timeout_task.run_coalesced(0)

emoji_re: Pattern[str] = re.compile(r"<a?:[A-Za-z0-9\_]+:[0-9]{13,20}>")

class PollView(View):
    def __init__(self, poll: Poll) -> None:
        super().__init__(timeout=None)

        self.add_item(Button(style=ButtonStyle.primary, label="Retract vote",
            custom_id="{}:{}:RetractVote".format(__name__, poll.message_id)))
        if poll.poll == PollType.WITH_CONCERNS:
            self.add_item(Button(style=ButtonStyle.primary, label="Raise concern",
                custom_id="{}:{}:RaiseConcern".format(__name__, poll.message_id)))
            self.add_item(Button(style=ButtonStyle.primary, label="Retract concern",
                custom_id="{}:{}:RetractConcern".format(__name__, poll.message_id)))
        self.add_item(Button(style=ButtonStyle.danger, label="Close",
            custom_id="{}:{}:Close".format(__name__, poll.message_id)))

        for i, name in enumerate(poll.options):
            if poll.poll in (PollType.WITH_COMMENTS, PollType.WITH_CONCERNS):
                custom_id = "{}:{}:VoteComment:{}".format(__name__, poll.message_id, i)
            else:
                custom_id = "{}:{}:Vote:{}".format(__name__, poll.message_id, i)
            row = 1 + i // 5
            if emoji_re.match(name):
                self.add_item(Button(style=ButtonStyle.success, emoji=discord.PartialEmoji.from_str(name),
                    custom_id=custom_id, row=row))
            else:
                self.add_item(Button(style=ButtonStyle.success, label=name, custom_id=custom_id, row=row))

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

def render_poll_individual(options: List[str], votes: Sequence[Vote], concerns: Optional[Sequence[Concern]]) -> str:
    rows = ["Votes and concerns:" if concerns is not None else "Votes:"]
    for item in merge_vote_concern(votes, concerns if concerns is not None else ()):
        if isinstance(item, Vote):
            row = format("{} {!m}", options[item.choice_index], item.voter_id)
            if item.comment:
                row += ": " + item.comment
        else:
            row = format("\u26A0 {!m}: {}", item.author_id, item.comment)
        rows.append(row)
    return "\n".join(rows)

def render_poll_summary(options: List[str], votes: Sequence[Vote], concerns: Optional[Sequence[Concern]]) -> str:
    rows = ["Votes and concerns:" if concerns is not None else "Votes:"]
    totals = [0] * len(options)
    for item in merge_vote_concern(votes, concerns if concerns is not None else ()):
        if isinstance(item, Vote):
            totals[item.choice_index] += 1
        else:
            if any(totals):
                rows.append(", ".join("{}: {}".format(option, total) for total, option in zip(totals, options)))
            rows.append(format("\u26A0 {!m}: {}", item.author_id, item.comment))
    if any(totals):
        rows.append(", ".join("{}: {}".format(option, total) for total, option in zip(totals, options)))
    return "\n".join(rows)

def render_poll(poll: Poll, votes: Sequence[Vote], concerns: Sequence[Concern]) -> str:
    content = ""
    if poll.poll != PollType.COUNTED:
        content = render_poll_individual(poll.options, votes, concerns if poll.poll == PollType.WITH_CONCERNS else None)
    if poll.poll == PollType.COUNTED or len(content) >= 2000:
        content = render_poll_summary(poll.options, votes, concerns if poll.poll == PollType.WITH_CONCERNS else None)
    return content[:2000]

async def sync_poll(session: AsyncSession, poll_id: int, msg: PartialMessage) -> None:
    poll = await session.get_one(Poll, poll_id)
    assert poll
    stmt = select(Vote).where(Vote.poll_id == poll_id).order_by(nulls_first(Vote.after_concern), Vote.id)
    votes = (await session.execute(stmt)).scalars().all()
    stmt = select(Concern).where(Concern.poll_id == poll_id).order_by(Concern.id)
    concerns = (await session.execute(stmt)).scalars().all()
    content = render_poll(poll, votes, concerns)
    view = PollView(poll)
    await retry(lambda: msg.edit(content=content, view=view, allowed_mentions=user_mentions))

async def cast_vote(interaction: Interaction, poll_id: int, choice_index: int, comment: str,
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
        session.add(Vote(poll_id=poll_id, voter_id=interaction.user.id, choice_index=choice_index,
            after_concern=after_concern, comment=comment))
        await session.commit()
        await sync_poll(session, poll_id, msg)
        if vote is None:
            text = "Vote added."
        else:
            text = "Vote updated."
        await interaction.response.send_message(text, ephemeral=True, delete_after=60)

async def retract_vote(interaction: Interaction, poll_id: int) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        if (msg := await poll.get_votes_message()) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        stmt = select(Vote).where(Vote.poll_id == poll_id, Vote.voter_id == interaction.user.id).limit(1)
        vote = (await session.execute(stmt)).scalar()
        if vote is None:
            text = "Nothing changed."
        else:
            await session.delete(vote)
            await session.commit()
            await sync_poll(session, poll_id, msg)
            text = "Vote retracted."
        await interaction.response.send_message(text, ephemeral=True, delete_after=60)

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
            await retry(lambda: msg.channel.send(" ".join(format("{!m}", vote.voter_id) for vote in votes),
                allowed_mentions=user_mentions, reference=msg))
        await interaction.response.send_message("Concern added.", ephemeral=True, delete_after=60)
        timeout_task.run_coalesced(1)

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
        await interaction.response.send_message("Concern retracted.", ephemeral=True, delete_after=60)

class VoteModal(Modal):
    def __init__(self, poll_id: int, choice_index: int, concerns: List[str], comment: str,
        after_concern: Optional[int]) -> None:
        self.poll_id = poll_id
        self.after_concern = after_concern
        self.choice_index = choice_index
        super().__init__(title="Vote", timeout=600)
        if concerns:
            self.add_item(TextInput(style=TextStyle.paragraph, required=False,
                label="Concerns raised since your last vote", default="\n\n".join(concerns)[:4000]))

        self.comment = TextInput(style=TextStyle.paragraph, required=False, max_length=300,
            label="Comment (optional)", default=comment)
        self.add_item(self.comment)

    async def on_submit(self, interaction: Interaction) -> None:
        await cast_vote(interaction, self.poll_id, self.choice_index, comment=str(self.comment)[:300],
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

async def prompt_vote_comment(interaction: Interaction, poll_id: int, choice_index: int) -> None:
    async with sessionmaker() as session:
        stmt = select(Vote).where(Vote.poll_id == poll_id, Vote.voter_id == interaction.user.id).limit(1)
        vote = (await session.execute(stmt)).scalar()
        stmt = select(Concern).where(Concern.poll_id == poll_id)
        concerns = (await session.execute(stmt)).scalars().all()
        await interaction.response.send_modal(VoteModal(poll_id, choice_index,
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
        await session.delete(poll)
        try:
            if (msg := await poll.get_votes_message()) is not None:
                await msg.edit(view=None)
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
        args = data["custom_id"].split(":")
        if len(args) < 3 or args[0] != __name__:
            return
        try:
            poll_id = int(args[1])
        except ValueError:
            return
        action = args[2]
        if action == "Vote":
            if len(args) == 3:
                if interaction.message:
                    async with sessionmaker() as session:
                        await sync_poll(session, poll_id, interaction.message)
            elif len(args) == 4:
                try:
                    choice_index = int(args[3])
                except ValueError:
                    return
                async with sessionmaker() as session:
                    stmt = select(Concern.id).where(Concern.poll_id == poll_id).order_by(Concern.id.desc()).limit(1)
                    after_concern = (await session.execute(stmt)).scalar_one_or_none()
                await cast_vote(interaction, poll_id, choice_index, "", after_concern)
        elif action == "VoteComment":
            if len(args) == 4:
                try:
                    choice_index = int(args[3])
                except ValueError:
                    return
                await prompt_vote_comment(interaction, poll_id, choice_index)
        elif action == "RetractVote":
            if len(args) == 3:
                await retract_vote(interaction, poll_id)
        elif action == "RaiseConcern":
            if len(args) == 3:
                await prompt_raise_concern(interaction, poll_id)
        elif action == "RetractConcern":
            if len(args) == 3:
                await prompt_retract_concern(interaction, poll_id)
        elif action == "Close":
            if len(args) == 3:
                await close_poll(interaction, poll_id)

    @cleanup
    @command("poll")
    @privileged
    async def poll(self, ctx: Context, kind: Literal["choice", "counted", "comments", "concerns"], options: str,
        duration: DurationConverter, *, comment: str) -> None:
        """
        Create a poll. Every person can choose one of the provided options, provided in a comma-separated list (possibly
        emojis). The following types of poll are supported:
        - `choice`: basic poll displaying what everyone voted for
        - `counted`: only display the number of votes for every option
        - `comments`: allow users to attach a comment to their vote
        - `concerns`: allow raising of concerns
        Concerns ping everyone who voted previously, prompting them to update their vote. After a given duration of
        inactivity, the owner of the poll will be prompted to close it.
        """
        if ctx.guild is None:
            raise UserError("This can only be used in a guild")
        if not comment:
            raise UserError("Poll comment must not be empty")
        option_list = options.split(",")
        if len(option_list) < 2:
            raise UserError("At least 2 options are required")
        if len(option_list) >= 20:
            raise UserError("At most 20 options are supported")

        if kind == "choice":
            kind_enum = PollType.CHOICE
        elif kind == "counted":
            kind_enum = PollType.COUNTED
        elif kind == "comments":
            kind_enum = PollType.WITH_COMMENTS
        elif kind == "concerns":
            kind_enum = PollType.WITH_CONCERNS

        async with sessionmaker() as session:
            msg = await ctx.channel.send(format("Poll by {!m}:\n\n{}", ctx.author, comment[:3000]),
                allowed_mentions=AllowedMentions(everyone=False, roles=False, users=[ctx.author]))

            if isinstance(ctx.channel, Thread):
                channel_id = ctx.channel.parent_id
                thread_id = ctx.channel.id
            else:
                channel_id = ctx.channel.id
                thread_id = None

            poll = Poll(poll=kind_enum, message_id=msg.id, votes_id=0, guild_id=ctx.guild.id, channel_id=channel_id,
                thread_id=thread_id, author_id=ctx.author.id, comment=comment, duration=int(duration.total_seconds()),
                timeout=datetime.utcnow() + duration, timeout_notified=False, options=option_list)

            votes = await ctx.channel.send(render_poll(poll, [], []), view=PollView(poll),
                allowed_mentions=user_mentions)

            poll.votes_id = votes.id
            session.add(poll)
            await session.commit()

            timeout_task.run_coalesced(1)

    @cleanup
    @command("polls")
    @privileged
    async def polls(self, ctx: Context) -> None:
        async with sessionmaker() as session:
            items = []
            stmt = select(Poll)
            for poll in (await session.execute(stmt)).scalars():
                channel_id = poll.channel_id if poll.thread_id is None else poll.thread_id
                channel = ctx.bot.get_partial_messageable(channel_id, guild_id=ctx.guild and ctx.guild.id)
                items.append(PlainItem(channel.get_partial_message(poll.message_id).jump_url + "\n"))
            for content, _ in chunk_messages(items):
                await ctx.send(content)
