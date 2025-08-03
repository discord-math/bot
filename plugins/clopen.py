from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import enum
import logging
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Union, cast

import discord
from discord import (
    AllowedMentions,
    ButtonStyle,
    CategoryChannel,
    Embed,
    ForumChannel,
    ForumTag,
    Interaction,
    InteractionType,
    Member,
    Message,
    Object,
    PartialMessage,
    RawMessageDeleteEvent,
    RawReactionActionEvent,
    SelectOption,
    TextChannel,
    TextStyle,
    Thread,
    User,
)
from discord.abc import GuildChannel
from discord.ext.commands import group
from discord.ui import Button, Modal, Select, TextInput, View
from sqlalchemy import ARRAY, TIMESTAMP, BigInteger, Enum, ForeignKey, select
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column, raiseload, relationship
from sqlalchemy.schema import CreateSchema

from bot.acl import EvalResult, evaluate_ctx, evaluate_interaction, privileged, register_action
from bot.client import client
from bot.cogs import Cog, cog, command
import bot.commands
from bot.commands import Context
from bot.config import plugin_config_command
import bot.message_tracker
from bot.tasks import task
import plugins
import util.db
from util.discord import (
    DurationConverter,
    PartialCategoryChannelConverter,
    PartialForumChannelConverter,
    PartialGuildConverter,
    PartialRoleConverter,
    PartialTextChannelConverter,
    PlainItem,
    Typing,
    UserError,
    chunk_messages,
    format,
)


if TYPE_CHECKING:
    import discord.types.interactions


def available_embed() -> Embed:
    checkmark_url = "https://cdn.discordapp.com/emojis/901284681633370153.png?size=256"
    helpers = 286206848099549185
    help_chan = 488120190538743810
    return Embed(
        color=0x7CB342,
        description=format(
            "Send your question here to claim the channel.\n\n"
            "Remember:\n"
            "• **Ask** your math question in a clear, concise manner.\n"
            "• **Show** your work, and if possible, explain where you are stuck.\n"
            "• **After 15 minutes**, feel free to ping {!M}.\n"
            "• Type the command {!i} to free the channel when you're done.\n"
            "• Be polite and have a nice day!\n\n"
            "Read {!c} for further information on how to ask a good question, "
            "and about conduct in the question channels.",
            helpers,
            bot.commands.prefix + "close",
            help_chan,
        ),
    ).set_author(name="Available help channel!", icon_url=checkmark_url)


def closed_embed(reason: str, reopen: bool) -> Embed:
    if reopen:
        reason += format("\n\nUse {!i} if this was a mistake.", bot.commands.prefix + "reopen")
    return Embed(color=0x000000, title="Channel closed", description=reason)


def solved_embed(reason: str) -> Embed:
    checkmark_url = "https://cdn.discordapp.com/emojis/1021392975449825322.webp?size=256&quality=lossless"
    return Embed(
        color=0x7CB342,
        description=format(
            "Post marked as solved {}.\n\nUse {!i} if this was a mistake.", reason, bot.commands.prefix + "unsolved"
        ),
    ).set_author(name="Solved", icon_url=checkmark_url)


def unsolved_embed(reason: str) -> Embed:
    ping_url = "https://cdn.discordapp.com/emojis/1021392792783683655.webp?size=256&quality=lossless"
    return Embed(
        color=0x7CB342,
        description=format(
            "Post marked as unsolved {}.\n\nUse {!i} to mark as solved.", reason, bot.commands.prefix + "solved"
        ),
    ).set_author(name="Unsolved", icon_url=ping_url)


def limit_embed() -> Embed:
    return Embed(color=0xB37C42, description="Please don't occupy multiple help channels.")


def prompt_message(mention: int) -> str:
    return format("{!m} Has your question been resolved?", mention)


registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)
logger = logging.getLogger(__name__)


@registry.mapped
class GuildConfig:
    __tablename__ = "guilds"
    __table_args__ = {"schema": "clopen"}

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    available_category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hidden_category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # How long initially until the channel becomes pending for closure after someone talking
    timeout: Mapped[timedelta] = mapped_column(INTERVAL, nullable=False)
    # How long initially until the channel becomes pending for closure after the owner talks
    owner_timeout: Mapped[timedelta] = mapped_column(INTERVAL, nullable=False)
    # Acceptable minimum number of channels in the available category at any time
    min_avail: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Acceptable maximum number of channels in the available category at any time
    max_avail: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Acceptable total max number of help channels (new ones will be created up to this limit).
    # If this exceeds 50, we will have trouble moving channels between categories.
    max_channels: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Max channels that can be simultaneously assigned to a single user at any time
    limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Role that is assigned whenever someone reaches the max number of channels.
    # This role should ideally prevent them from posting in the available category.
    limit_role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    forum_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Forum posts that cannot be messaged
    pinned_posts_ids: Mapped[List[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    solved_tag_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unsolved_tag_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    channels: Mapped[List[Channel]] = relationship("Channel", lazy="joined", back_populates="guild")

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            guild_id: int,
            available_category_id: int,
            used_category_id: int,
            hidden_category_id: int,
            timeout: timedelta,
            owner_timeout: timedelta,
            min_avail: int,
            max_avail: int,
            max_channels: int,
            limit: int,
            limit_role_id: int,
            forum_id: int,
            pinned_posts_ids: List[int],
            solved_tag_id: int,
            unsolved_tag_id: int,
        ) -> None: ...


class ChannelState(enum.Enum):
    AVAILABLE = "available"
    USED = "used"
    PENDING = "pending"
    CLOSED = "closed"
    HIDDEN = "hidden"


@registry.mapped
class Channel:
    __tablename__ = "channels"
    __table_args__ = {"schema": "clopen"}

    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(GuildConfig.guild_id), nullable=False)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[ChannelState] = mapped_column(Enum(ChannelState, schema="clopen"))
    # User ID of the last owner of the channel
    owner_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # If AVAILABLE, ID of the message with the available embed.
    # Otherwise ID of the message prompting for channel closure.
    prompt_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # ID of the message that is the original post
    op_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # How much to multiply timeout/owner_timeout by
    extension: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # When to transition to the respective next state
    expiry: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

    guild: Mapped[GuildConfig] = relationship(GuildConfig, lazy="joined")

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            guild_id: int,
            id: int,
            index: int,
            state: ChannelState,
            extension: int,
            owner_id: Optional[int] = ...,
            prompt_id: Optional[int] = ...,
            op_id: Optional[int] = ...,
            expiry: Optional[datetime] = ...,
        ) -> None: ...


manage_clopen = register_action("manage_clopen")

channel_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


@task(name="Clopen scheduler task", exc_backoff_base=10)
async def scheduler_task() -> None:
    await client.wait_until_ready()

    async with sessionmaker() as session:
        stmt = select(GuildConfig)
        configs = list((await session.execute(stmt)).scalars().unique())

        for config in configs:
            if sum(channel.state == ChannelState.AVAILABLE for channel in config.channels) < config.min_avail:
                for channel in config.channels:
                    async with channel_locks[channel.id]:
                        if channel.state == ChannelState.HIDDEN:
                            await make_available(session, channel)
                            break
                else:
                    if (channel := await create_channel(session, config)) is not None:
                        await make_available(session, channel)

        min_next = None
        for config in configs:
            for channel in config.channels:
                async with channel_locks[channel.id]:
                    if channel.state == ChannelState.USED and channel.expiry is not None:
                        if channel.expiry < datetime.utcnow():
                            await make_pending(session, channel)
                        elif min_next is None or channel.expiry < min_next:
                            min_next = channel.expiry
                    elif channel.state == ChannelState.PENDING and channel.expiry is not None:
                        if channel.expiry < datetime.utcnow():
                            await close(session, channel, "Closed due to timeout")
                        elif min_next is None or channel.expiry < min_next:
                            min_next = channel.expiry
                    elif channel.state == ChannelState.CLOSED:
                        if channel.expiry is None or channel.expiry < datetime.utcnow():
                            if (
                                sum(channel.state == ChannelState.AVAILABLE for channel in config.channels)
                                >= config.max_avail
                            ):
                                await make_hidden(session, channel)
                            else:
                                await make_available(session, channel)
                        elif min_next is None or channel.expiry < min_next:
                            min_next = channel.expiry

    if min_next is not None:
        scheduler_task.run_coalesced((min_next - datetime.utcnow()).total_seconds())


@plugins.init
async def init() -> None:
    global scheduler_task
    await util.db.init(util.db.get_ddl(CreateSchema("clopen"), registry.metadata.create_all))

    scheduler_task.run_coalesced(0)

    await bot.message_tracker.subscribe(__name__, None, process_messages, missing=True, retroactive=False)

    async def unsubscribe() -> None:
        await bot.message_tracker.unsubscribe(__name__, None)

    plugins.finalizer(unsubscribe)


rename_tasks: Dict[int, asyncio.Task[object]] = {}
last_rename: Dict[int, datetime] = {}


def request_rename(chan: TextChannel, name: str) -> None:
    if chan.id in rename_tasks and not rename_tasks[chan.id].done():
        rename_tasks[chan.id].cancel()

    async def do_rename(chan: TextChannel, name: str) -> None:
        try:
            await chan.edit(name=name)
        except asyncio.CancelledError:
            raise
        except:
            last_rename[chan.id] = datetime.utcnow()
        else:
            last_rename[chan.id] = datetime.utcnow()

    rename_tasks[chan.id] = asyncio.create_task(do_rename(chan, name))


async def insert_chan(conf: GuildConfig, cat_id: int, chan: TextChannel, *, beginning: bool = False) -> None:
    channels = [channel.id for channel in sorted(conf.channels, key=lambda channel: channel.index)]
    assert chan.id in channels
    cat = await client.fetch_channel(cat_id)
    assert isinstance(cat, CategoryChannel)

    max_chan = None
    if not beginning:
        for other in sorted(cat.channels, key=lambda chan: chan.position):
            if other.id in channels and channels.index(other.id) >= channels.index(chan.id):
                break
            max_chan = other
    if max_chan is None:
        await chan.move(category=cat, sync_permissions=True, beginning=True)
    else:
        await chan.move(category=cat, sync_permissions=True, after=max_chan)


async def update_owner_limit(conf: GuildConfig, user_id: int) -> bool:
    assert isinstance(cat := client.get_channel(conf.used_category_id), GuildChannel)
    user = cat.guild.get_member(user_id)
    if user is None:
        return False
    has_role = any(role.id == conf.limit_role_id for role in user.roles)
    reached_limit = (
        sum(
            channel.owner_id == user_id and channel.state in (ChannelState.USED, ChannelState.PENDING)
            for channel in conf.channels
        )
        >= conf.limit
    )
    try:
        if reached_limit and not has_role:
            logger.debug("Adding limiting role for {}".format(user_id))
            await user.add_roles(Object(conf.limit_role_id))
        elif not reached_limit and has_role:
            logger.debug("Removing limiting role for {}".format(user_id))
            await user.remove_roles(Object(conf.limit_role_id))
    except (discord.NotFound, discord.Forbidden):
        pass
    return reached_limit


async def occupy(session: AsyncSession, channel: Channel, msg_id: int, author: Union[User, Member]) -> None:
    logger.debug("Occupying {}, author {}, OP {}".format(channel.id, author.id, msg_id))
    assert isinstance(chan := client.get_channel(channel.id), TextChannel)
    assert channel.state == ChannelState.AVAILABLE
    assert (conf := await session.get(GuildConfig, channel.guild_id))
    await session.refresh(conf, attribute_names=("channels",))
    channel.state = ChannelState.USED
    channel.owner_id = author.id
    old_op_id = channel.op_id
    channel.op_id = msg_id
    channel.extension = 1
    channel.expiry = datetime.utcnow() + channel.guild.owner_timeout
    await session.commit()
    await enact_occupied(conf, chan, author, op_id=msg_id, old_op_id=old_op_id)
    scheduler_task.run_coalesced(0)


async def enact_occupied(
    conf: GuildConfig,
    channel: TextChannel,
    owner: Union[User, Member],
    *,
    op_id: Optional[int],
    old_op_id: Optional[int],
) -> None:
    reached_limit = await update_owner_limit(conf, owner.id)
    try:
        if old_op_id is not None:
            await PartialMessage(channel=channel, id=old_op_id).unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        if op_id is not None:
            await PartialMessage(channel=channel, id=op_id).pin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        await insert_chan(conf, conf.used_category_id, channel, beginning=True)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix + "\uFF5C" + owner.display_name)
    except discord.Forbidden:
        pass
    if reached_limit:
        await channel.send(embed=limit_embed(), allowed_mentions=AllowedMentions.none())


async def keep_occupied(session: AsyncSession, channel: Channel, msg_author_id: int) -> None:
    logger.debug("Bumping {} by {}".format(channel.id, msg_author_id))
    assert channel.state == ChannelState.USED
    if msg_author_id == channel.owner_id:
        new_expiry = datetime.utcnow() + channel.guild.owner_timeout * channel.extension
    else:
        new_expiry = datetime.utcnow() + channel.guild.timeout * channel.extension
    if (old_expiry := channel.expiry) is None or old_expiry < new_expiry:
        channel.expiry = new_expiry
    await session.commit()


async def close(session: AsyncSession, channel: Channel, reason: str, *, reopen: bool = True) -> None:
    logger.debug("Closing {}, reason {!r}, reopen={!r}".format(channel.id, reason, reopen))
    assert isinstance(chan := client.get_channel(channel.id), TextChannel)
    assert channel.state in (ChannelState.USED, ChannelState.PENDING)
    channel.state = ChannelState.CLOSED
    now = datetime.utcnow()
    channel.expiry = max(
        now + timedelta(seconds=60),
        last_rename.get(channel.id, now) + timedelta(seconds=600),  # channel rename ratelimit
    )
    old_op_id = channel.op_id
    old_owner_id = channel.owner_id
    if not reopen:
        channel.owner_id = None
        channel.op_id = None
    if old_owner_id is not None:
        assert (conf := await session.get(GuildConfig, channel.guild_id))
        await session.refresh(conf, attribute_names=("channels",))
        await update_owner_limit(conf, old_owner_id)
    try:
        if not reopen and old_op_id is not None:
            await PartialMessage(channel=chan, id=old_op_id).unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        if channel.prompt_id is not None:
            assert client.user is not None
            await PartialMessage(channel=chan, id=channel.prompt_id).remove_reaction("\u274C", client.user)
    except (discord.NotFound, discord.Forbidden):
        pass
    await chan.send(embed=closed_embed(reason, reopen), allowed_mentions=AllowedMentions.none())
    await session.commit()
    scheduler_task.run_coalesced(0)


async def make_available(session: AsyncSession, channel: Channel) -> None:
    logger.debug("Making {} available".format(channel.id))
    assert isinstance(chan := client.get_channel(channel.id), TextChannel)
    assert (conf := await session.get(GuildConfig, channel.guild_id))
    await session.refresh(conf, attribute_names=("channels",))
    assert channel.state in (ChannelState.CLOSED, ChannelState.HIDDEN)
    channel.state = ChannelState.AVAILABLE
    channel.expiry = None
    channel.prompt_id = await enact_available(conf, chan)
    await session.commit()
    scheduler_task.run_coalesced(0)


async def enact_available(conf: GuildConfig, chan: TextChannel) -> int:
    try:
        await insert_chan(conf, conf.available_category_id, chan)
        prefix = chan.name.split("\uFF5C", 1)[0]
        request_rename(chan, prefix)
    except discord.Forbidden:
        pass
    return (await chan.send(embed=available_embed(), allowed_mentions=AllowedMentions.none())).id


async def make_hidden(session: AsyncSession, channel: Channel) -> None:
    logger.debug("Making {} hidden".format(channel.id))
    assert isinstance(chan := client.get_channel(channel.id), TextChannel)
    assert (conf := await session.get(GuildConfig, channel.guild_id))
    await session.refresh(conf, attribute_names=("channels",))
    assert channel.state in (ChannelState.AVAILABLE, ChannelState.CLOSED)
    channel.state = ChannelState.HIDDEN
    channel.expiry = None
    channel.prompt_id = None
    await enact_hidden(conf, chan)
    await session.commit()
    scheduler_task.run_coalesced(0)


async def enact_hidden(conf: GuildConfig, channel: TextChannel) -> None:
    try:
        await insert_chan(conf, conf.hidden_category_id, channel)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix)
    except discord.Forbidden:
        pass


async def create_channel(session: AsyncSession, conf: GuildConfig) -> Optional[Channel]:
    if len(conf.channels) >= conf.max_channels:
        return None
    logger.debug("Creating a new channel")
    cat = client.get_channel(conf.hidden_category_id)
    assert isinstance(cat, CategoryChannel)
    try:
        chan = await cat.create_text_channel(name="help-{}".format(len(conf.channels)))
        logger.debug("Created a new channel: {}".format(chan.id))
        channel = Channel(
            guild_id=conf.guild_id,
            index=len(conf.channels),
            id=chan.id,
            state=ChannelState.HIDDEN,
            extension=1,
        )
        session.add(channel)
        await session.commit()
        return channel
    except discord.Forbidden:
        return None


async def extend(session: AsyncSession, channel: Channel) -> None:
    assert isinstance(chan := client.get_channel(channel.id), TextChannel)
    assert channel.state == ChannelState.PENDING
    channel.extension *= 2
    logger.debug("Extending {} to {}x".format(channel.id, channel.extension))
    channel.expiry = datetime.utcnow() + channel.guild.owner_timeout * channel.extension
    channel.state = ChannelState.USED
    try:
        if (prompt_id := channel.prompt_id) is not None:
            assert client.user is not None
            await PartialMessage(channel=chan, id=prompt_id).remove_reaction("\u2705", client.user)
    except (discord.NotFound, discord.Forbidden):
        pass
    await session.commit()
    scheduler_task.run_coalesced(0)


async def make_pending(session: AsyncSession, channel: Channel) -> None:
    logger.debug("Prompting {} for closure".format(channel.id))
    assert isinstance(chan := client.get_channel(channel.id), TextChannel)
    assert channel.state == ChannelState.USED
    assert (owner_id := channel.owner_id) is not None
    channel.expiry = datetime.utcnow() + channel.guild.owner_timeout * channel.extension
    prompt = await chan.send(prompt_message(owner_id))
    await prompt.add_reaction("\u2705")
    await prompt.add_reaction("\u274C")
    channel.prompt_id = prompt.id
    channel.state = ChannelState.PENDING
    await session.commit()
    scheduler_task.run_coalesced(0)


async def reopen(session: AsyncSession, channel: Channel) -> None:
    logger.debug("Reopening {}".format(channel.id))
    assert isinstance(chan := client.get_channel(channel.id), TextChannel)
    assert channel.state in (ChannelState.AVAILABLE, ChannelState.CLOSED)
    assert (owner_id := channel.owner_id) is not None
    prompt_id = channel.prompt_id if channel.state == ChannelState.AVAILABLE else None
    channel.state = ChannelState.USED
    channel.expiry = datetime.utcnow() + channel.guild.owner_timeout * channel.extension
    assert (conf := await session.get(GuildConfig, channel.guild_id))
    await session.refresh(conf, attribute_names=("channels",))
    await update_owner_limit(conf, owner_id)
    try:
        if prompt_id is not None:
            await PartialMessage(channel=chan, id=prompt_id).delete()
    except (discord.NotFound, discord.Forbidden):
        pass
    try:
        await insert_chan(conf, conf.used_category_id, chan, beginning=True)
        prefix = chan.name.split("\uFF5C", 1)[0]
        author = await chan.guild.fetch_member(owner_id)
        request_rename(chan, prefix + "\uFF5C" + author.display_name)
    except (discord.NotFound, discord.Forbidden):
        pass
    await session.commit()
    scheduler_task.run_coalesced(0)


async def synchronize_channels() -> List[str]:
    output = []
    async with sessionmaker() as session:
        stmt = select(GuildConfig)
        for conf in (await session.execute(stmt)).scalars().unique():
            assert isinstance(cat := client.get_channel(conf.used_category_id), GuildChannel)
            for channel in conf.channels:
                chan = client.get_channel(channel.id)
                if not isinstance(chan, TextChannel):
                    output.append(format("{!c} is not a text channel", channel.id))
                    continue
                if channel.state == ChannelState.AVAILABLE:
                    if chan.category is None or chan.category.id != conf.available_category_id:
                        output.append(format("{!c} moved to the available category", channel.id))
                        await enact_available(conf, chan)
                    else:
                        valid_prompt = channel.prompt_id
                        if valid_prompt is not None:
                            try:
                                if not (await chan.fetch_message(valid_prompt)).embeds:
                                    valid_prompt = None
                            except (discord.NotFound, discord.Forbidden):
                                valid_prompt = None
                        if valid_prompt is None:
                            output.append(format("Posted available message in {!c}", channel.id))
                            msg = await chan.send(embed=available_embed(), allowed_mentions=AllowedMentions.none())
                            channel.prompt_id = msg.id
                            await session.commit()
                        else:
                            async for msg in chan.history(limit=None, after=Object(valid_prompt)):
                                if not msg.author.bot:
                                    output.append(format("{!c} assigned to {!m}", channel.id, msg.author))
                                    await occupy(session, channel, msg.id, msg.author)
                                    break
                elif channel.state in (ChannelState.USED, ChannelState.PENDING):
                    op_id = channel.op_id
                    owner_id = channel.owner_id
                    owner = cat.guild.get_member(owner_id) if owner_id is not None else None
                    if owner is None:
                        output.append(format("{!c} has no owner, closed", channel.id))
                        await close(session, channel, "The owner is missing!", reopen=False)
                    elif op_id is None:
                        output.append(format("{!c} has no OP message, closed", channel.id))
                        await close(session, channel, "The original message is missing!", reopen=False)
                    elif chan.category is None or chan.category.id != conf.used_category_id:
                        output.append(format("{!c} moved to the used category", channel.id))
                        await enact_occupied(conf, chan, owner, op_id=op_id, old_op_id=None)
                elif channel.state == ChannelState.CLOSED:
                    if chan.category is None or chan.category.id != conf.used_category_id:
                        output.append(format("{!c} moved to the used category", channel.id))
                        await insert_chan(conf, conf.used_category_id, chan, beginning=True)
                elif channel.state == ChannelState.HIDDEN:
                    if chan.category is None or chan.category.id != conf.hidden_category_id:
                        output.append(format("{!c} moved to the hidden category", channel.id))
                        await insert_chan(conf, conf.hidden_category_id, chan, beginning=True)
            if (role := cat.guild.get_role(conf.limit_role_id)) is not None:
                for user in role.members:
                    if not await update_owner_limit(conf, user.id):
                        output.append(format("Removed limiting role from {!m}", user))
            for channel in conf.channels:
                chan = client.get_channel(channel.id)
                if not isinstance(channel, TextChannel):
                    continue
                for msg in await channel.pins():
                    if (
                        channel.state
                        not in (ChannelState.AVAILABLE, ChannelState.USED, ChannelState.PENDING, ChannelState.CLOSED)
                        or msg.id != channel.op_id
                    ):
                        output.append(format("Removed extraneous pin from {!c}", id))
                        await msg.unpin()
    return output


async def set_solved_tags(conf: GuildConfig, post: Thread, new_tags: Iterable[int], reason: str) -> None:
    solved_tags = [conf.solved_tag_id, conf.unsolved_tag_id]
    tags = [tag for tag in post.applied_tags if tag.id not in solved_tags]
    tags += [cast(ForumTag, Object(id)) for id in new_tags]
    try:
        await post.edit(applied_tags=tags, reason=reason)
    except discord.HTTPException:
        logger.error(format("Could not set solved tags on {!c}", post), exc_info=True)


async def solved(conf: GuildConfig, post: Thread, reason: str) -> None:
    if any(tag.id == conf.solved_tag_id for tag in post.applied_tags):
        return
    await set_solved_tags(conf, post, [conf.solved_tag_id], reason)
    await post.send(embed=solved_embed(reason), allowed_mentions=AllowedMentions.none())


async def unsolved(conf: GuildConfig, post: Thread, reason: str) -> None:
    if not any(tag.id == conf.solved_tag_id for tag in post.applied_tags):
        return
    await set_solved_tags(conf, post, [conf.unsolved_tag_id], reason)
    await post.send(embed=unsolved_embed(reason), allowed_mentions=AllowedMentions.none())


async def wait_close_post(post: Thread, reason: str) -> None:
    await asyncio.sleep(300)  # TODO: what if the post is reopened in the meantime?
    await post.edit(archived=True, reason=reason)


class PostTagsView(View):
    def __init__(self, post: Thread) -> None:
        assert isinstance(post.parent, ForumChannel)
        super().__init__(timeout=None)

        options = [
            SelectOption(label=tag.name, value=str(tag.id), emoji=tag.emoji, default=tag in post.applied_tags)
            for tag in post.parent.available_tags
            if not tag.moderated
        ]

        self.add_item(
            Select(
                placeholder="Select tags for this post...",
                min_values=0,
                max_values=min(4, len(options)),  # 5 sans 1 for solved/unsolved
                options=options,
                custom_id="{}:tags:{}".format(__name__, post.id),
            )
        )

        self.add_item(
            Button(style=ButtonStyle.secondary, label="Rename post", custom_id="{}:title:{}".format(__name__, post.id))
        )


class PostTitleModal(Modal):
    def __init__(self, post: Thread) -> None:
        super().__init__(title="Edit post title", timeout=600)
        self.thread = post
        self.name = TextInput(
            style=TextStyle.short,
            placeholder="Enter post title...",
            label="Post title",
            default=post.name,
            required=True,
            max_length=100,
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: Interaction) -> None:
        try:
            await self.thread.edit(name=str(self.name), reason=format("By {!m}", interaction.user))
        except discord.HTTPException:
            return
        await interaction.response.send_message("\u2705", ephemeral=True, delete_after=60)


async def manage_title(interaction: Interaction, thread_id: int) -> None:
    try:
        thread = await interaction.client.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden):
        return
    if not isinstance(thread, Thread):
        return
    if not isinstance(thread.parent, ForumChannel):
        return
    if not interaction.message:
        return

    if thread.owner_id != interaction.user.id:
        if manage_clopen.evaluate(*evaluate_interaction(interaction)) != EvalResult.TRUE:
            await interaction.response.send_message(
                "You cannot edit the title on this post", ephemeral=True, delete_after=60
            )
            return

    await interaction.response.send_modal(PostTitleModal(thread))


async def manage_tags(interaction: Interaction, thread_id: int, values: List[str]) -> None:
    try:
        thread = await interaction.client.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden):
        return
    if not isinstance(thread, Thread):
        return
    if not isinstance(thread.parent, ForumChannel):
        return
    if not interaction.message:
        return

    if thread.owner_id != interaction.user.id:
        if manage_clopen.evaluate(*evaluate_interaction(interaction)) != EvalResult.TRUE:
            await interaction.response.send_message(
                "You cannot edit tags on this post", ephemeral=True, delete_after=60
            )
            return

    id_values = []
    for v in values:
        try:
            id_values.append(int(v))
        except ValueError:
            continue

    async with sessionmaker() as session:
        if not (conf := await session.get(GuildConfig, thread.guild.id, options=(raiseload(GuildConfig.channels),))):
            return
    solved_tags = [conf.solved_tag_id, conf.unsolved_tag_id]
    tags = [tag for tag in thread.applied_tags if tag.id in solved_tags]
    tags += [tag for tag in thread.parent.available_tags if not tag.moderated and tag.id in id_values]

    try:
        new_thread = await thread.edit(applied_tags=tags, reason=format("By {!m}", interaction.user))
    except discord.HTTPException:
        return
    await interaction.message.edit(view=PostTagsView(new_thread))
    await interaction.response.send_message("\u2705", ephemeral=True, delete_after=60)


async def process_messages(msgs: Iterable[Message]) -> None:
    async with sessionmaker() as session:
        for msg in msgs:
            if msg.author.bot:
                continue
            if not msg.guild:
                continue
            if not (conf := await session.get(GuildConfig, msg.guild.id, options=(raiseload(GuildConfig.channels),))):
                continue

            if msg.channel.id in conf.pinned_posts_ids:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass

            if isinstance(msg.channel, Thread) and msg.channel.parent_id == conf.forum_id:
                if msg.id == msg.channel.id:  # starter post in a thread
                    await set_solved_tags(conf, msg.channel, [conf.unsolved_tag_id], "new post")
                    await msg.channel.send(view=PostTagsView(msg.channel))


@cog
class ClopenCog(Cog):
    @Cog.listener()
    async def on_ready(self) -> None:
        output = await synchronize_channels()
        if output:
            logger.error("\n".join(output))

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        if msg.author.bot:
            return
        if not msg.guild:
            return
        async with sessionmaker() as session:
            if not (channel := await session.get(Channel, msg.channel.id)):
                return
            async with channel_locks[channel.id]:
                if channel.state == ChannelState.USED:
                    await keep_occupied(session, channel, msg.author.id)
                elif channel.state == ChannelState.AVAILABLE:
                    if not msg.content.startswith(bot.commands.prefix):
                        await occupy(session, channel, msg.id, msg.author)

    @Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        async with sessionmaker() as session:
            if not (channel := await session.get(Channel, payload.channel_id)):
                return
            if payload.message_id != channel.prompt_id:
                return
            if payload.user_id != channel.owner_id:
                return
            async with channel_locks[channel.id]:
                if channel.state == ChannelState.PENDING:
                    if payload.emoji.name == "\u2705":
                        await close(session, channel, format("Closed by {!m}", payload.user_id))
                    elif payload.emoji.name == "\u274C":
                        await extend(session, channel)

    @Cog.listener()
    async def on_raw_message_delete(self, payload: RawMessageDeleteEvent) -> None:
        async with sessionmaker() as session:
            if not (channel := await session.get(Channel, payload.channel_id)):
                return
            if payload.message_id != channel.op_id:
                return
            async with channel_locks[payload.channel_id]:
                if channel.state in (ChannelState.USED, ChannelState.PENDING):
                    await close(
                        session,
                        channel,
                        "Channel closed due to the original message being deleted. \n"
                        "If you did not intend to do this, please **open a new help channel**, \n"
                        "as this action is irreversible, and this channel may abruptly lock.",
                        reopen=False,
                    )
                else:
                    channel.owner_id = None
                    await session.commit()

    @Cog.listener()
    async def on_interaction(self, interaction: Interaction) -> None:
        if interaction.type != InteractionType.component or interaction.data is None:
            return
        data = cast("discord.types.interactions.MessageComponentInteractionData", interaction.data)
        if data["component_type"] == 3:
            if ":" not in data["custom_id"]:
                return
            mod, rest = data["custom_id"].split(":", 1)
            if mod != __name__ or ":" not in rest:
                return
            action, thread_id = rest.split(":", 1)
            try:
                thread_id = int(thread_id)
            except ValueError:
                return
            if action == "tags":
                await manage_tags(interaction, thread_id, data["values"])
        elif data["component_type"] == 2:
            if ":" not in data["custom_id"]:
                return
            mod, rest = data["custom_id"].split(":", 1)
            if mod != __name__ or ":" not in rest:
                return
            action, thread_id = rest.split(":", 1)
            try:
                thread_id = int(thread_id)
            except ValueError:
                return
            if action == "title":
                await manage_title(interaction, thread_id)

    @privileged
    @command("close", aliases=["solved"])
    async def close_command(self, ctx: Context) -> None:
        """For use in help channels and help forum posts. Close a channel and/or mark the post as solved."""
        if not ctx.guild:
            return
        async with sessionmaker() as session:
            if isinstance(ctx.channel, Thread):
                conf = await session.get(GuildConfig, ctx.channel.guild.id, options=(raiseload(GuildConfig.channels),))
                if not conf:
                    return
                if ctx.channel.parent_id != conf.forum_id:
                    return
                if ctx.author.id != ctx.channel.owner_id:
                    if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                        return
                await solved(conf, ctx.channel, format("by {!m}", ctx.author))
                asyncio.create_task(wait_close_post(ctx.channel, format("Closed by {!m}", ctx.author)))
            else:
                if not (channel := await session.get(Channel, ctx.channel.id)):
                    return
                if ctx.author.id != channel.owner_id:
                    if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                        return
                async with channel_locks[ctx.channel.id]:
                    if channel.state in (ChannelState.USED, ChannelState.PENDING):
                        await close(session, channel, format("Closed by {!m}", ctx.author))

    @privileged
    @command("reopen", aliases=["unsolved"])
    async def reopen_command(self, ctx: Context) -> None:
        """For use in help channels and help forum posts. Reopen a recently closed channel and/or mark the post as
        unsolved."""
        if not ctx.guild:
            return
        async with sessionmaker() as session:
            if isinstance(ctx.channel, Thread):
                conf = await session.get(GuildConfig, ctx.channel.guild.id, options=(raiseload(GuildConfig.channels),))
                if not conf:
                    return
                if ctx.channel.parent_id != conf.forum_id:
                    return
                if ctx.author.id != ctx.channel.owner_id:
                    if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                        return
                await unsolved(conf, ctx.channel, format("by {!m}", ctx.author))
            else:
                if not (channel := await session.get(Channel, ctx.channel.id)):
                    return
                if ctx.author.id != channel.owner_id:
                    if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                        return
                async with channel_locks[ctx.channel.id]:
                    if channel.state in (ChannelState.CLOSED, ChannelState.AVAILABLE):
                        if channel.owner_id is not None:
                            await reopen(session, channel)
                            await ctx.send("\u2705")

    @privileged
    @command("clopen_sync")
    async def clopen_sync_command(self, ctx: Context) -> None:
        """Try and synchronize the state of clopen channels with Discord in case of errors or outages."""
        async with Typing(ctx):
            output = await synchronize_channels()
        if output:
            for content, _ in chunk_messages(PlainItem(text + "\n") for text in output):
                await ctx.send(content, allowed_mentions=AllowedMentions.none())
        else:
            await ctx.send("\u2705", allowed_mentions=AllowedMentions.none())


async def is_channel_owned_by(session: AsyncSession, chan: Union[GuildChannel, Thread], user_id: int) -> Optional[bool]:
    if isinstance(chan, Thread):
        if not (conf := await session.get(GuildConfig, chan.guild.id, options=(raiseload(GuildConfig.channels),))):
            return None
        if chan.parent_id != conf.forum_id:
            return None
        return chan.owner_id == user_id
    else:
        if not (channel := await session.get(Channel, chan.id, options=(raiseload(Channel.guild),))):
            return None
        return channel.owner_id == user_id


class GuildContext(Context):
    guild_id: int


@plugin_config_command
@group("clopen")
@privileged
async def config(ctx: GuildContext, server: PartialGuildConverter) -> None:
    ctx.guild_id = server.id


@config.command("new")
@privileged
async def config_new(
    ctx: GuildContext,
    available_category: PartialCategoryChannelConverter,
    used_category: PartialCategoryChannelConverter,
    hidden_category: PartialCategoryChannelConverter,
    limit_role: PartialRoleConverter,
    forum: PartialForumChannelConverter,
    solved_tag_id: int,
    unsolved_tag_id: int,
) -> None:
    async with sessionmaker() as session:
        session.add(
            GuildConfig(
                guild_id=ctx.guild_id,
                available_category_id=available_category.id,
                used_category_id=used_category.id,
                hidden_category_id=hidden_category.id,
                timeout=timedelta(seconds=60),
                owner_timeout=timedelta(seconds=60),
                min_avail=1,
                max_avail=1,
                max_channels=0,
                limit=1,
                limit_role_id=limit_role.id,
                forum_id=forum.id,
                pinned_posts_ids=[],
                solved_tag_id=solved_tag_id,
                unsolved_tag_id=unsolved_tag_id,
            )
        )
        await session.commit()
        await ctx.send("\u2705")


async def get_conf(session: AsyncSession, ctx: GuildContext, load_channels: bool = False) -> GuildConfig:
    options = None if load_channels else (raiseload(GuildConfig.channels),)
    if (conf := await session.get(GuildConfig, ctx.guild_id, options=options)) is None:
        raise UserError("No config for {}".format(ctx.guild_id))
    return conf


@config.command("available")
@privileged
async def config_available(ctx: GuildContext, category: Optional[PartialCategoryChannelConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if category is None:
            await ctx.send(format("{!c}", conf.available_category_id))
        else:
            conf.available_category_id = category.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("used")
@privileged
async def config_used(ctx: GuildContext, category: Optional[PartialCategoryChannelConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if category is None:
            await ctx.send(format("{!c}", conf.used_category_id))
        else:
            conf.used_category_id = category.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("hidden")
@privileged
async def config_hidden(ctx: GuildContext, category: Optional[PartialCategoryChannelConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if category is None:
            await ctx.send(format("{!c}", conf.hidden_category_id))
        else:
            conf.hidden_category_id = category.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("timeout")
@privileged
async def config_timeout(ctx: GuildContext, duration: Optional[DurationConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if duration is None:
            await ctx.send(str(conf.timeout))
        else:
            conf.timeout = duration
            await session.commit()
            await ctx.send("\u2705")


@config.command("owner_timeout")
@privileged
async def config_owner_timeout(ctx: GuildContext, duration: Optional[DurationConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if duration is None:
            await ctx.send(str(conf.owner_timeout))
        else:
            conf.owner_timeout = duration
            await session.commit()
            await ctx.send("\u2705")


@config.command("min_avail")
@privileged
async def config_min_avail(ctx: GuildContext, number: Optional[int]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if number is None:
            await ctx.send(str(conf.min_avail))
        else:
            conf.min_avail = number
            await session.commit()
            await ctx.send("\u2705")


@config.command("max_avail")
@privileged
async def config_max_avail(ctx: GuildContext, number: Optional[int]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if number is None:
            await ctx.send(str(conf.max_avail))
        else:
            conf.max_avail = number
            await session.commit()
            await ctx.send("\u2705")


@config.command("max_channels")
@privileged
async def config_max_channels(ctx: GuildContext, number: Optional[int]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if number is None:
            await ctx.send(str(conf.max_channels))
        else:
            conf.max_channels = number
            await session.commit()
            await ctx.send("\u2705")


@config.command("limit")
@privileged
async def config_limit(ctx: GuildContext, number: Optional[int]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if number is None:
            await ctx.send(str(conf.limit))
        else:
            conf.limit = number
            await session.commit()
            await ctx.send("\u2705")


@config.command("limit_role")
@privileged
async def config_limit_role(ctx: GuildContext, role: Optional[PartialRoleConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if role is None:
            await ctx.send(format("{!M}", conf.limit_role_id), allowed_mentions=AllowedMentions.none())
        else:
            conf.limit_role_id = role.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("forum")
@privileged
async def config_forum(ctx: GuildContext, forum: Optional[PartialForumChannelConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if forum is None:
            await ctx.send(format("{!c}", conf.forum_id), allowed_mentions=AllowedMentions.none())
        else:
            conf.forum_id = forum.id
            await session.commit()
            await ctx.send("\u2705")


@config.group("pinned", invoke_without_command=True)
@privileged
async def config_pinned(ctx: GuildContext) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        await ctx.send(", ".join(format("{!c}", id) for id in conf.pinned_posts_ids))


@config_pinned.command("add")
@privileged
async def config_pinned_add(ctx: GuildContext, post_id: int) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        conf.pinned_posts_ids = list(set(conf.pinned_posts_ids) | {post_id})
        await session.commit()
        await ctx.send("\u2705")


@config_pinned.command("remove")
@privileged
async def config_pinned_remove(ctx: GuildContext, post_id: int) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        conf.pinned_posts_ids = list(set(conf.pinned_posts_ids) - {post_id})
        await session.commit()
        await ctx.send("\u2705")


@config.command("solved_tag")
@privileged
async def config_solved_tag(ctx: GuildContext, tag_id: Optional[int]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if tag_id is None:
            await ctx.send(str(conf.solved_tag_id))
        else:
            conf.solved_tag_id = tag_id
            await session.commit()
            await ctx.send("\u2705")


@config.command("unsolved_tag")
@privileged
async def config_unsolved_tag(ctx: GuildContext, tag_id: Optional[int]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if tag_id is None:
            await ctx.send(str(conf.unsolved_tag_id))
        else:
            conf.unsolved_tag_id = tag_id
            await session.commit()
            await ctx.send("\u2705")


@config.group("channels", invoke_without_command=True)
@privileged
async def config_channels(ctx: GuildContext) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx, load_channels=True)
        await ctx.send(
            "\n".join(
                format("{!c}", channel.id) for channel in sorted(conf.channels, key=lambda channel: channel.index)
            )
            or "No channels registered"
        )


@config_channels.command("add")
@privileged
async def config_channels_add(ctx: GuildContext, channel: PartialTextChannelConverter) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx, load_channels=True)
        session.add(
            Channel(
                guild_id=conf.guild_id,
                id=channel.id,
                index=len(conf.channels),
                state=ChannelState.HIDDEN,
                extension=1,
            )
        )
        await session.commit()
        await ctx.send("\u2705")
