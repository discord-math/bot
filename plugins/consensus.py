import asyncio
import datetime
import enum
import logging
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, List, Optional, Sequence, Union, cast

import discord
import discord.app_commands
import discord.ext.commands
if TYPE_CHECKING:
    import discord.types.interactions
import discord.ui
import sqlalchemy
import sqlalchemy.ext.asyncio
import sqlalchemy.orm

import bot.client
import bot.cogs
import bot.commands
import bot.locations
import bot.privileges
import plugins
import util.db
import util.discord

logger = logging.getLogger(__name__)

user_mentions = discord.AllowedMentions(everyone=False, roles=False, users=True)

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = sqlalchemy.ext.asyncio.async_sessionmaker(engine, future=True, expire_on_commit=False)

@registry.mapped
class Poll:
    __tablename__ = "polls"
    __table_args__ = {"schema": "consensus"}

    message_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, primary_key=True)
    votes_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    guild_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    channel_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    thread_id: sqlalchemy.orm.Mapped[Optional[int]] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger)
    author_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    comment: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT, nullable=False)
    duration: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    timeout: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(sqlalchemy.TIMESTAMP,
        nullable=False)
    timeout_notified: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(sqlalchemy.BOOLEAN, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, message_id: int, votes_id: int, guild_id: int, channel_id: int, thread_id: Optional[int],
            author_id: int, comment: str, duration: int, timeout: datetime.datetime, timeout_notified: bool) -> None:
            ...

    async def get_votes_message(self) -> Optional[discord.PartialMessage]:
        channel_id = self.channel_id if self.thread_id is None else self.thread_id
        try:
            if not isinstance(channel := await bot.client.client.fetch_channel(channel_id),
                discord.abc.Messageable):
                return None
        except (discord.NotFound, discord.Forbidden):
            return None
        return channel.get_partial_message(self.votes_id)

@registry.mapped
class Concern:
    __tablename__ = "concerns"
    __table_args__ = {"schema": "consensus"}

    id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.Integer, primary_key=True)
    poll_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger,
        sqlalchemy.ForeignKey(Poll.message_id, ondelete="CASCADE"), nullable=False) # type: ignore
    author_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    comment: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT, nullable=False)

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

    id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.Integer, primary_key=True)
    poll_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger,
        sqlalchemy.ForeignKey(Poll.message_id, ondelete="CASCADE"), nullable=False) # type: ignore
    voter_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    vote: sqlalchemy.orm.Mapped[VoteType] = sqlalchemy.orm.mapped_column(
        sqlalchemy.Enum(VoteType, schema="consensus"), nullable=False)
    after_concern: sqlalchemy.orm.Mapped[Optional[int]] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger)
    comment: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, poll_id: int, voter_id: int, vote: VoteType, after_concern: Optional[int], comment: str
            ) -> None: ...

timeout_event = asyncio.Event()

def timeouts_updated() -> None:
    timeout_event.set()

async def handle_timeouts() -> None:
    await bot.client.client.wait_until_ready()

    while True:
        try:
            async with sessionmaker() as session:
                stmt = sqlalchemy.select(Poll).where(Poll.timeout_notified == False)
                polls = (await session.execute(stmt)).scalars().all()

                min_timeout = None
                now = datetime.datetime.utcnow()
                try:
                    for poll in polls:
                        if poll.timeout <= now:
                            if (msg := await poll.get_votes_message()) is None:
                                await session.delete(poll)
                            else:
                                try:
                                    await msg.channel.send(util.discord.format("Poll timed out {!m}", poll.author_id),
                                        allowed_mentions=user_mentions, reference=msg)
                                except discord.HTTPException:
                                    pass
                            poll.timeout_notified = True
                        else:
                            if min_timeout is None or poll.timeout < min_timeout:
                                min_timeout = poll.timeout
                finally:
                    await session.commit()

            delay = (min_timeout - datetime.datetime.utcnow()).total_seconds() if min_timeout is not None else 86400.0
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
        sqlalchemy.schema.CreateSchema("consensus"),
        registry.metadata.create_all))
    timeout_task = asyncio.create_task(handle_timeouts())
    plugins.finalizer(timeout_task.cancel)

class PollView(discord.ui.View):
    def __init__(self, poll_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.primary, label="Vote",
            custom_id="{}:{}:Vote".format(__name__, poll_id)))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.primary, label="Raise concern",
            custom_id="{}:{}:RaiseConcern".format(__name__, poll_id)))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.primary, label="Retract concern",
            custom_id="{}:{}:RetractConcern".format(__name__, poll_id)))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.danger, label="Close",
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
            row = util.discord.format("{!m}: {}", item.voter_id, emoji)
            if item.comment:
                row += " " + item.comment
        else:
            row = util.discord.format("\u26A0 {!m}: {}", item.author_id, item.comment)
        rows.append(row)
    return "\n".join(rows)

async def edit_poll(poll_id: Optional[int], msg: discord.PartialMessage, votes: Sequence[Vote],
    concerns: Sequence[Concern]) -> None:
    await msg.edit(content=render_poll(votes, concerns)[:4000], view=PollView(poll_id) if poll_id is not None else None,
        allowed_mentions=user_mentions)

async def sync_poll(session: sqlalchemy.ext.asyncio.AsyncSession, poll_id: int, msg: discord.PartialMessage):
    stmt = (sqlalchemy.select(Vote)
        .where(Vote.poll_id == poll_id)
        .order_by(sqlalchemy.sql.expression.nullsfirst(Vote.after_concern), Vote.id))
    votes = (await session.execute(stmt)).scalars().all()
    stmt = (sqlalchemy.select(Concern)
        .where(Concern.poll_id == poll_id)
        .order_by(Concern.id))
    concerns = (await session.execute(stmt)).scalars().all()
    await edit_poll(poll_id, msg, votes, concerns)


async def cast_vote(interaction: discord.Interaction, poll_id: int, vote_type: Optional[VoteType], comment: str,
    after_concern: Optional[int]) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        if (msg := await poll.get_votes_message()) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        stmt = (sqlalchemy.select(Vote)
            .where(Vote.poll_id == poll_id, Vote.voter_id == interaction.user.id)
            .limit(1))
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

async def raise_concern(interaction: discord.Interaction, poll_id: int, comment: str) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        if (msg := await poll.get_votes_message()) is None:
            await interaction.response.send_message("Poll does not exist.", ephemeral=True)
            return
        session.add(Concern(poll_id=poll_id, author_id=interaction.user.id, comment=comment))
        poll.timeout = datetime.datetime.utcnow() + datetime.timedelta(seconds=poll.duration)
        poll.timeout_notified = False
        await session.commit()
        await sync_poll(session, poll_id, msg)
        stmt = (sqlalchemy.select(Vote)
            .where(Vote.poll_id == poll_id)
            .order_by(Vote.id))
        votes = (await session.execute(stmt)).scalars().all()
        if len(votes):
            await msg.channel.send(" ".join(util.discord.format("{!m}", vote.voter_id) for vote in votes),
                allowed_mentions=user_mentions, reference=msg)
        await interaction.response.send_message("Concern added.", ephemeral=True)
        timeouts_updated()

async def retract_concern(interaction: discord.Interaction, poll_id: int, id: int) -> None:
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

class VoteModal(discord.ui.Modal):
    def __init__(self, poll_id: int, concerns: List[str], vote: Optional[VoteType], comment: str,
        after_concern: Optional[int]) -> None:
        self.poll_id = poll_id
        self.after_concern = after_concern
        super().__init__(title="Vote", timeout=600)
        if concerns:
            self.add_item(discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False,
                label="Concerns raised since your last vote", default="\n\n".join(concerns)[:4000]))

        # Selects not allowed in modals for now
        #self.vote = discord.ui.Select(placeholder="Vote", min_values=1, max_values=1, options=[
        #    discord.SelectOption(label="Upvote", emoji="\u2705", default=vote==VoteType.UPVOTE),
        #    discord.SelectOption(label="Neutral", emoji="\U0001F518", default=vote==VoteType.NEUTRAL),
        #    discord.SelectOption(label="Downvote", emoji="\u274C", default=vote==VoteType.DOWNVOTE),
        #    discord.SelectOption(label="None (retract vote)")])
        self.vote = discord.ui.TextInput(required=False, max_length=1,
            label="[Y]es\u2705 / [N]o\u274C / [A]bstain \U0001F518 / [R]etract \u21A9")
        if vote == VoteType.UPVOTE:
            self.vote.default = "Y"
        elif vote == VoteType.NEUTRAL:
            self.vote.default = "A"
        elif vote == VoteType.DOWNVOTE:
            self.vote.default = "N"
        self.add_item(self.vote)

        self.comment = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=300,
            label="Comment (optional)", default=comment)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
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

class RaiseConcernModal(discord.ui.Modal):
    concern = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=True, max_length=300, label="Concern")

    def __init__(self, poll_id: int) -> None:
        self.poll_id = poll_id
        super().__init__(title="Raise concern", timeout=600)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.concern):
            await raise_concern(interaction, self.poll_id, str(self.concern)[:300])

class RetractConcernModal(discord.ui.Modal):
    def __init__(self, poll_id: int, concerns: Dict[int, str]) -> None:
        self.poll_id = poll_id
        super().__init__(title="Raise concern", timeout=600)
        # Selects not available in modals for now:
        # self.concern = discord.ui.Select(placeholder="Select the concern to retract", min_values=1, max_values=1,
        #    options=[discord.SelectOption(label=concern[:300], value=str(key)) for key, concern in concerns.items()])
        self.keys = list(concerns)
        self.concern = discord.ui.TextInput(required=True,
            label="Which concern to retract? {}...{}".format(1, len(concerns)))
        self.add_item(self.concern)

    async def on_submit(self, interaction: discord.Interaction) -> None:
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

async def prompt_vote(interaction: discord.Interaction, poll_id: int) -> None:
    async with sessionmaker() as session:
        stmt = (sqlalchemy.select(Vote)
            .where(Vote.poll_id == poll_id, Vote.voter_id == interaction.user.id)
            .limit(1))
        vote = (await session.execute(stmt)).scalar()
        stmt = (sqlalchemy.select(Concern)
            .where(Concern.poll_id == poll_id))
        concerns = (await session.execute(stmt)).scalars().all()
        await interaction.response.send_modal(VoteModal(poll_id, vote=None if vote is None else vote.vote,
            concerns=[concern.comment for concern in concerns
                if vote is not None and (vote.after_concern is None or concern.id > vote.after_concern)],
            comment="" if vote is None else vote.comment, after_concern=concerns[-1].id if concerns else None))

async def prompt_raise_concern(interaction: discord.Interaction, poll_id: int) -> None:
    await interaction.response.send_modal(RaiseConcernModal(poll_id))

async def prompt_retract_concern(interaction: discord.Interaction, poll_id: int) -> None:
    async with sessionmaker() as session:
        stmt = (sqlalchemy.select(Concern)
            .where(Concern.poll_id == poll_id, Concern.author_id == interaction.user.id))
        concerns = {}
        for concern in (await session.execute(stmt)).scalars():
            concerns[concern.id] = concern.comment
        if concerns:
            await interaction.response.send_modal(RetractConcernModal(poll_id, concerns))

async def close_poll(interaction: discord.Interaction, poll_id: int) -> None:
    async with sessionmaker() as session:
        if (poll := await session.get(Poll, poll_id)) is None:
            return
        if poll.author_id != interaction.user.id:
            return
        stmt = (sqlalchemy.select(Vote)
            .where(Vote.poll_id == poll_id)
            .order_by(sqlalchemy.sql.expression.nullsfirst(Vote.after_concern), Vote.id))
        votes = (await session.execute(stmt)).scalars().all()
        stmt = (sqlalchemy.select(Concern)
            .where(Concern.poll_id == poll_id)
            .order_by(Concern.id))
        concerns = (await session.execute(stmt)).scalars().all()
        await session.delete(poll)
        try:
            if (msg := await poll.get_votes_message()) is not None:
                await edit_poll(None, msg, votes, concerns)
        except discord.HTTPException:
            pass
        await session.commit()


@bot.cogs.cog
class ConsensusCog(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component or interaction.data is None:
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

    @bot.commands.cleanup
    @discord.ext.commands.command("poll")
    @bot.locations.location("poll")
    async def poll(self, ctx: bot.commands.Context, duration: util.discord.DurationConverter, *,
        comment: str) -> None:
        """
        Create a poll with the specified timeout duration and the given message.
        """
        if ctx.guild is None:
            raise util.discord.UserError("This can only be used in a guild")
        if not comment:
            raise util.discord.UserError("Poll comment must not be empty")
        async with sessionmaker() as session:
            msg = await ctx.channel.send(util.discord.format("Poll by {!m}:\n\n{}", ctx.author, comment[:3000]),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[ctx.author]))
            votes = await ctx.channel.send(render_poll([], []), view=PollView(msg.id),
                allowed_mentions=user_mentions)

            if isinstance(ctx.channel, discord.Thread):
                channel_id = ctx.channel.parent_id
                thread_id = ctx.channel.id
            else:
                channel_id = ctx.channel.id
                thread_id = None
            session.add(Poll(message_id=msg.id, votes_id=votes.id, guild_id=ctx.guild.id, channel_id=channel_id,
                thread_id=thread_id, author_id=ctx.author.id, comment=comment, duration=int(duration.total_seconds()),
                timeout=datetime.datetime.utcnow() + duration, timeout_notified=False))
            await session.commit()
            timeouts_updated()

    @bot.commands.cleanup
    @discord.ext.commands.command("polls")
    @bot.privileges.priv("mod")
    async def polls(self, ctx: bot.commands.Context) -> None:
        async with sessionmaker() as session:
            output = ""
            stmt = sqlalchemy.select(Poll)
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
