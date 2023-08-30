from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import enum
import logging
import re
from typing import (TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, Iterator, List, NamedTuple,
    Optional, Protocol, Sequence, Set, Tuple, Type, TypeVar, Union, cast)

import discord
from discord import (AllowedMentions, AuditLogAction, AuditLogEntry, ChannelType, Embed, Member, Message,
    MessageReference, Object, PartialMessage, TextChannel, Thread, User, VoiceState)
from discord.abc import Messageable
import sqlalchemy
from sqlalchemy import (TEXT, TIMESTAMP, BigInteger, Column, ForeignKey, Integer, MetaData,
    PrimaryKeyConstraint, Table, func, select)
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, joinedload, mapped_column, relationship
from sqlalchemy.schema import DDL, CreateSchema

from bot.client import client
from bot.cogs import Cog, cog, command, group
import bot.commands
from bot.commands import Context, cleanup
from bot.privileges import priv
from bot.reactions import ReactionMonitor, get_input
import plugins
import plugins.persistence
import util.db
import util.db.kv
from util.discord import (InvocationError, PartialUserConverter, PlainItem, UserError, chunk_messages, format,
    parse_duration)
from util.frozen_list import FrozenList

logger: logging.Logger = logging.getLogger(__name__)

# ----------- Config -----------
class TicketsConf(Awaitable[None], Protocol):
    guild: int # ID of the guild the ticket system is managing
    tracked_roles: FrozenList[int] # List of roleids of tracked roles
    last_auditid: Optional[int] # ID of last audit event processed
    ticket_list: int # Channel id of the ticket list in the guild
    prompt_interval: int # How often to ping about delivered tickets
    pending_unmutes: FrozenList[int] # List of users peding VC unmute
    pending_undeafens: FrozenList[int] # List of users peding VC undeafen
    audit_log_precision: float # How long to allow the audit log to catch up
    cleanup_delay: Optional[float] # How long to wait before cleaning up junk messages in the ticket list

conf: TicketsConf

cleanup_exempt: Set[int] = set()

@plugins.init
async def init_conf() -> None:
    global conf
    conf = cast(TicketsConf, await util.db.kv.load(__name__))

# ----------- Data -----------

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True, expire_on_commit=False)

class TicketType(enum.Enum):
    """
    The possible ticket types.
    Types are represented as the corresponding moderation action.
    """
    NOTE = "Note"
    KICK = "Kick"
    BAN = "Ban"
    VC_MUTE = "VC Mute"
    VC_DEAFEN = "VC Deafen"
    ADD_ROLE = "Role Added"
    TIMEOUT = "Timeout"

class TicketStatus(enum.Enum):
    """
    Possible values for the current status of a moderation action.
    """
    # Ticket currently active
    IN_EFFECT = "In Effect"
    # Ticket's duration has expired
    EXPIRED = "Expired"
    # Ticket's duration has expired but we couldn't revert it for whatever reason
    EXPIRE_FAILED = "Expiration failed"
    # Ticket has been manually reverted
    REVERTED = "Manually reverted"
    # Ticket is inactive and has been hidden
    HIDDEN = "Hidden"

class TicketStage(enum.Enum):
    """
    The possible stages of delivery of a ticket to the responsible moderator.
    """
    NEW = "New"
    DELIVERED = "Delivered"
    COMMENTED = "Commented"

ModQueueView = Table("mod_queues", MetaData(), Column("id", BigInteger), schema="tickets")

perm_duration_re = re.compile(r"""p(?:erm(?:anent)?)?[^\w'"]*""", re.IGNORECASE)

@registry.mapped
class TicketMod:
    __tablename__ = "mods"
    __table_args__ = {"schema": "tickets"}
    modid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_read_msgid: Mapped[Optional[int]] = mapped_column(BigInteger)
    scheduled_delivery: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)

    queue_top = relationship(lambda: Ticket,
            primaryjoin=lambda: sqlalchemy.and_(TicketMod.modid == Ticket.modid,
                Ticket.id.in_(select(ModQueueView.columns.id))),
        viewonly=True, uselist=False) # type: Mapped[Optional[Ticket]]
        # needs to be refreshed whenever ticket.stage is updated

    if TYPE_CHECKING:
        def __init__(self, *, modid: int, last_read_msgid: Optional[int] = ...,
            scheduled_delivery: Optional[datetime] = ...) -> None: ...

    @staticmethod
    async def get(session: AsyncSession, modid: int) -> TicketMod:
        """Get a TicketMod by id, or create if it doesn't exist"""
        mod: Optional[TicketMod] = await session.get(TicketMod, modid)
        if mod is None:
            mod = TicketMod(modid=modid)
            logger.debug("Creating TicketMod {}".format(modid))
            session.add(mod)
        return mod

    async def load_queue(self) -> Optional[Ticket]:
        """Populate the queue_top field"""
        await async_object_session(self).get(TicketMod, self.modid, # type: ignore
            populate_existing=True, options=(joinedload(TicketMod.queue_top),))
        return self.queue_top

    @staticmethod
    async def update_delivered_message(ticket: Ticket) -> None:
        if (msg := await ticket.get_delivery_message()) is not None:
            await msg.edit(content=msg.content, embed=ticket.to_embed(dm=True))

    async def ticket_updated(self, ticket: Ticket) -> None:
        """Update the prompt DM if there is one"""
        if ticket == await self.load_queue():
            await self.update_delivered_message(ticket)
            delivery_updated()

    async def transfer(self, ticket: Ticket, modid: int, *, actorid: int) -> None:
        """
        Transfer ticket to a new owner. Notify the old owner if they had a prompt for it. Shuffle the new owner's queue.
        """
        logger.debug("Transferring Ticket #{} from {} to {}".format(ticket.id, self.modid, modid))
        if ticket == await self.load_queue():
            if (msg := await ticket.get_delivery_message()) is not None:
                try:
                    await msg.channel.send(format("Ticket #{} was taken by {!m}", ticket.id, modid))
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        ticket.modified_by = actorid
        ticket.delivered_id = None
        new_mod = await TicketMod.get(async_object_session(self), modid) # type: ignore
        old_top = await new_mod.load_queue()
        ticket.mod = new_mod
        if ticket == await new_mod.load_queue():
            if old_top is not None:
                if (msg := await old_top.get_delivery_message()) is not None:
                    try:
                        await msg.channel.send("Ticket #{} is no longer at the top of your queue".format(old_top.id))
                        await msg.delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass
                old_top.stage = TicketStage.NEW
                old_top.delivered_id = None
            new_mod.scheduled_delivery = None

    @staticmethod
    def format_delivery(ticket: Ticket, related: Sequence[Ticket]) -> Tuple[str, list[Embed]]:
        embeds = [ticket.to_embed(dm=True)]

        def item_gen() -> Iterator[PlainItem]:
            first = True
            for ticket in related:
                if first:
                    yield PlainItem("User has outstanding tickets: ")
                else:
                    yield PlainItem(", ")
                first = False
                yield PlainItem(format("[#{}]({}): {} ({})", ticket.id, ticket.jump_link,
                    ticket.describe(target=False, mod=False, dm=True), ticket.status_line))

        for content, _ in chunk_messages(item_gen()):
            embeds.append(Embed(description=content, color=0xFF9900))
        if ticket.can_revert:
            return "Set a duration and a comment (e.g. 1 day 8 hours, breaking rules) on the following:", embeds
        else:
            return "Set a comment on the following:", embeds

    delivery_comment = "Please set a duration/comment on the following:"

    async def try_initial_delivery(self, ticket: Ticket, related: Sequence[Ticket]) -> None:
        logger.debug(format("Delivering Ticket #{} to {!m}", ticket.id, self.modid))
        user = client.get_user(self.modid)
        if user is None:
            try:
                user = await client.fetch_user(self.modid)
            except discord.NotFound:
                logger.error(format("Could not find {!m} to deliver Ticket #{}", self.modid, ticket.id))
                self.scheduled_delivery = datetime.utcnow() + timedelta(seconds=conf.prompt_interval)
                return
        try:
            if user.bot:
                return
            content, embeds = self.format_delivery(ticket, related)
            msg = await user.send(content, embeds=embeds)
        except (discord.NotFound, discord.Forbidden):
            return
        finally:
            self.scheduled_delivery = datetime.utcnow() + timedelta(seconds=conf.prompt_interval)
        ticket.delivered_id = msg.id
        ticket.stage = TicketStage.DELIVERED

    async def try_redelivery(self, ticket: Ticket, related: Sequence[Ticket]) -> None:
        logger.debug(format("Re-delivering Ticket #{} to {!m}", ticket.id, self.modid))
        user = client.get_user(self.modid)
        if user is None:
            try:
                user = await client.fetch_user(self.modid)
            except discord.NotFound:
                logger.error(format("Could not find {!m} to re-deliver Ticket #{}", self.modid, ticket.id))
                return
        if (msg := await ticket.get_delivery_message(user)) is not None:
            try:
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        try:
            if user.bot:
                return
            content, embeds = self.format_delivery(ticket, related)
            msg = await user.send(content, embeds=embeds)
        except (discord.NotFound, discord.Forbidden):
            return
        finally:
            self.scheduled_delivery = datetime.utcnow() + timedelta(seconds=conf.prompt_interval)
        ticket.delivered_id = msg.id
        ticket.stage = TicketStage.DELIVERED

    @staticmethod
    def parse_duration_comment(text: str) -> Tuple[Optional[int], bool, str]:
        if match := re.match(perm_duration_re, text):
            return None, True, text[match.end():]
        else:
            delta, offset = parse_duration(text)
            if offset:
                return delta // timedelta(seconds=1), True, text[offset:]
            else:
                return None, False, text

    async def process_message(self, msg: Message) -> None:
        """
        Process a non-command message from the moderator.
        If there is a current active ticket, treat it as a comment.
        Either way, update the last handled message in data.
        """
        prefix = bot.commands.conf.prefix
        if not prefix or not msg.content.startswith(prefix):
            if (ticket := await self.load_queue()) is not None:
                logger.debug(format("Processing message from {!m} as comment to Ticket #{}: {!r}",
                    self.modid, ticket.id, msg.content))

                duration, have_duration, ticket.comment = self.parse_duration_comment(msg.content)
                duration, have_duration, message = ticket.duration_message(duration, have_duration)
                if have_duration:
                    await ticket.set_duration(duration)

                ticket.stage = TicketStage.COMMENTED
                ticket.modified_by = self.modid
                self.scheduled_delivery = None

                try:
                    await msg.channel.send("Ticket comment set! " + message)
                except (discord.NotFound, discord.Forbidden):
                    pass

                await self.update_delivered_message(ticket)
                delivery_updated()

        self.last_read_msgid = msg.id

@registry.mapped
class Ticket:
    __allow_unmapped__ = True
    __tablename__ = "tickets"
    __table_args__ = {"schema": "tickets"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[TicketType] = mapped_column(sqlalchemy.Enum(TicketType, schema="tickets"), nullable=False)
    stage: Mapped[TicketStage] = mapped_column(sqlalchemy.Enum(TicketStage, schema="tickets"), nullable=False,
        default=TicketStage.NEW)
    status: Mapped[TicketStatus] = mapped_column(sqlalchemy.Enum(TicketStatus, schema="tickets"), nullable=False,
        default=TicketStatus.IN_EFFECT)
    modid: Mapped[int] = mapped_column(BigInteger, ForeignKey(TicketMod.modid), nullable=False)
    targetid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    auditid: Mapped[Optional[int]] = mapped_column(BigInteger)
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    comment: Mapped[Optional[str]] = mapped_column(TEXT)
    list_msgid: Mapped[Optional[int]] = mapped_column(BigInteger)
    delivered_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False,
        default=func.current_timestamp().op("AT TIME ZONE")("UTC"))
    modified_by: Mapped[Optional[int]] = mapped_column(BigInteger)

    mod: Mapped[TicketMod] = relationship(TicketMod, lazy="joined")
    __mapper_args__ = {"polymorphic_on": type}

    # Does this ticket type support reverting
    can_revert: bool

    # Action triggering automatic ticket creation
    trigger_action: Optional[AuditLogAction] = None
    # Action triggering automatic ticket update
    update_trigger_action: Optional[AuditLogAction] = None
    # Action triggering automatic ticket reversal
    revert_trigger_action: Optional[AuditLogAction] = None

    # The uncommented/in action/expired colors of the ticket embed
    colors: Tuple[int, int, int]

    @property
    def hidden(self) -> bool:
        return self.status == TicketStatus.HIDDEN

    @property
    def expiry(self) -> Optional[datetime]:
        if self.can_revert and self.duration is not None:
            return self.created_at + timedelta(seconds=self.duration)
        return None

    @property
    def jump_link(self) -> str:
        return 'https://discord.com/channels/{}/{}/{}'.format(conf.guild, conf.ticket_list, self.list_msgid)

    @property
    def status_line(self) -> str:
        if self.stage != TicketStage.COMMENTED:
            return self.status.value + ", Uncommented"
        return self.status.value

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        raise NotImplementedError

    def append_comment(self, comment: str) -> None:
        if self.comment is None:
            self.comment = comment
        else:
            self.comment += "\n" + comment

    async def set_duration(self, duration: Optional[int]) -> None:
        self.duration = duration

    def to_embed(self, *, dm: bool = False) -> Embed:
        """
        The discord embed describing this ticket.
        """
        if self.status == TicketStatus.EXPIRE_FAILED:
            color = 0x0000FF
        elif self.status == TicketStatus.HIDDEN:
            color = 0x000000
        elif self.status in (TicketStatus.EXPIRED, TicketStatus.REVERTED):
            color = self.colors[2]
        elif self.stage == TicketStage.COMMENTED:
            color = self.colors[1]
        else:
            color = self.colors[0]

        embed = Embed(
            title="Ticket #{}".format(self.id),
            description="{} ({})\n{}".format(self.describe(mod=False, dm=dm), self.status_line, self.comment or ""),
            timestamp=self.created_at, color=color)
        embed.add_field(name="Moderator", value=format("{!m}", self.modid))

        if self.can_revert:
            if self.expiry is None:
                embed.add_field(name="Permanent", value="\u200E")
            else:
                timestamp = int(self.expiry.replace(tzinfo=timezone.utc).timestamp())
                embed.add_field(name="Duration", value=str(timedelta(seconds=self.duration or 0)))
                embed.add_field(name="Expires", value="<t:{}:f>, <t:{}:R>".format(timestamp, timestamp))
        return embed

    async def publish(self) -> None:
        """
        Ticket update hook.
        Should be run whenever a ticket is created or updated.
        Manages the ticket list embed.
        Defers to the expiry and ticket mod update hooks.
        """
        logger.debug("Publishing Ticket #{}".format(self.id))

        # Reschedule or cancel ticket expiry if required
        expiry_updated()

        # Post to or update the ticket list
        if conf.ticket_list:
            channel = client.get_channel(conf.ticket_list)
            if isinstance(channel, (TextChannel, Thread)):
                message = None
                if self.list_msgid is not None:
                    try:
                        message = await channel.fetch_message(self.list_msgid)
                    except discord.NotFound:
                        pass

                if message is not None:
                    if not self.hidden:
                        try:
                            await message.edit(embed=self.to_embed())
                        except discord.HTTPException:
                            message = None
                    else:
                        try:
                            await message.delete()
                            self.list_msgid = None
                        except discord.HTTPException:
                            pass

                if message is None and not self.hidden:
                    message = await channel.send(embed=self.to_embed())
                    self.list_msgid = message.id
                    cleanup_exempt.add(message.id)

        # Run mod ticket update hook
        await self.mod.ticket_updated(self)

    @staticmethod
    @asynccontextmanager
    async def publish_all(session: AsyncSession) -> AsyncIterator[None]:
        """
        When entering, we save the list of all tickets that have been modified in this session. Presumably a commit
        happens after. When exiting we publish all those tickets. This can modify TicketMods so another commit might
        be needed afterwards.
        """
        tickets = []
        for obj in session.dirty:
            if isinstance(obj, Ticket):
                tickets.append(obj)
        for obj in session.new:
            if isinstance(obj, Ticket):
                tickets.append(obj)
        yield None
        for ticket in tickets:
            await ticket.publish()

    async def get_related(self, session: AsyncSession) -> Sequence[Ticket]:
        stmt = select(Ticket).where(Ticket.targetid == self.targetid, Ticket.id != self.id,
            Ticket.status != TicketStatus.HIDDEN)
        return (await session.execute(stmt)).scalars().all()

    def duration_message(self, duration: Optional[int], have_duration: bool) -> Tuple[Optional[int], bool, str]:
        if not have_duration:
            duration = None
        msg = ""
        if not self.can_revert:
            duration = None
            if have_duration:
                msg += "Provided duration ignored since this ticket type cannot expire."
            else:
                have_duration = True
        elif self.status != TicketStatus.IN_EFFECT:
            duration = None
            if have_duration:
                msg += "Provided duration ignored since this ticket is no longer in effect."
            else:
                have_duration = True
        elif duration is not None:
            expiry = self.created_at + timedelta(seconds=duration)
            now = datetime.utcnow()
            if expiry <= now:
                msg += "Ticket will expire immediately!"
            else:
                msg += "Ticket will expire in {}.".format(str(expiry - now).split('.')[0])
        return duration, have_duration, msg

    @staticmethod
    async def create_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        """
        If the audit log entry represents a mod action we care about, create a ticket and return it
        """
        return ()

    @staticmethod
    async def update_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        """
        If the audit log entry updates any tickets we have, return the list of such (updated) tickets.
        """
        return ()

    @staticmethod
    async def revert_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        """
        If the audit log entry represents any tickets we have, return the list of such tickets.
        """
        return ()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        """
        Attempt to reverse the ticket moderation action.
        Transparently re-raise exceptions.
        """
        raise NotImplementedError

    async def expire(self) -> None:
        """
        Automatically expire the ticket.
        """
        logger.debug("Expiring Ticket #{}".format(self.id))
        try:
            await self.revert_action(reason="Ticket #{}: Automatic expiry.".format(self.id))
        except:
            self.status = TicketStatus.EXPIRE_FAILED
            self.modified_by = None
            raise
        else:
            self.status = TicketStatus.EXPIRED
            self.modified_by = None

    async def revert(self, actorid: int) -> None:
        """
        Manually revert the ticket.
        """
        logger.debug("Manually reverting Ticket #{}".format(self.id))
        await self.revert_action(reason="Ticket #{}: Moderator {} requested revert.".format(self.id, actorid))
        self.status = TicketStatus.REVERTED
        self.modified_by = actorid

    async def hide(self, actorid: int, reason: Optional[str] = None) -> None:
        """
        Revert a ticket and set its status to HIDDEN.
        """
        logger.debug("Hiding Ticket #{}".format(self.id))
        if self.status not in (TicketStatus.EXPIRED, TicketStatus.REVERTED):
            await self.revert_action(reason="Ticket #{}: Moderator {} hid the ticket.".format(self.id, actorid))
        self.status = TicketStatus.HIDDEN
        self.modified_by = actorid
        if reason is not None:
            self.append_comment(reason)

    async def get_delivery_message(self, user: Optional[User] = None) -> Optional[Message]:
        if self.delivered_id is None:
            return None
        if user is None:
            user = client.get_user(self.modid)
        if user is None:
            try:
                user = await client.fetch_user(self.modid)
            except discord.NotFound:
                return None
        try:
            return await user.fetch_message(self.delivered_id)
        except discord.NotFound:
            return None

@registry.mapped
class TicketHistory:
    __tablename__ = "history"
    version: Mapped[int] = mapped_column(Integer)
    last_modified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey(Ticket.id, onupdate="CASCADE"))
    type: Mapped[Optional[TicketType]] = mapped_column(sqlalchemy.Enum(TicketType, schema="tickets"))
    stage: Mapped[Optional[TicketStage]] = mapped_column(sqlalchemy.Enum(TicketStage, schema="tickets"))
    status: Mapped[Optional[TicketStatus]] = mapped_column(sqlalchemy.Enum(TicketStatus, schema="tickets"))
    modid: Mapped[Optional[int]] = mapped_column(BigInteger)
    targetid: Mapped[Optional[int]] = mapped_column(BigInteger)
    roleid: Mapped[Optional[int]] = mapped_column(BigInteger)
    auditid: Mapped[Optional[int]] = mapped_column(BigInteger)
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    comment: Mapped[Optional[str]] = mapped_column(TEXT)
    list_msgid: Mapped[Optional[int]] = mapped_column(BigInteger)
    delivered_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    modified_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    __table_args__ = (PrimaryKeyConstraint(id, version), {"schema": "tickets"})

    @staticmethod
    async def get(session: AsyncSession, id: int) -> List[TicketHistory]:
        """Get history of a ticket by id, in chronological order"""
        stmt = select(TicketHistory).where(TicketHistory.id == id
            ).order_by(TicketHistory.version)
        return list((await session.execute(stmt)).scalars())


# Map of audit actions to the associated handler methods.
create_handlers: Dict[AuditLogAction, List[Callable[[AsyncSession, AuditLogEntry], Awaitable[Sequence[Ticket]]]]] = {}
update_handlers: Dict[AuditLogAction, List[Callable[[AsyncSession, AuditLogEntry], Awaitable[Sequence[Ticket]]]]] = {}
revert_handlers: Dict[AuditLogAction, List[Callable[[AsyncSession, AuditLogEntry], Awaitable[Sequence[Ticket]]]]] = {}

T = TypeVar("T", bound=Ticket)

# Decorator to register Ticket subclasses in action_handlers
def register_action(cls: Type[T]) -> Type[T]:
    if (action := cls.trigger_action) is not None:
        create_handlers.setdefault(action, []).append(cls.create_from_audit)
    if (action := cls.update_trigger_action) is not None:
        update_handlers.setdefault(action, []).append(cls.update_from_audit)
    if (action := cls.revert_trigger_action) is not None:
        revert_handlers.setdefault(action, []).append(cls.revert_from_audit)
    return cls

@registry.mapped
@register_action
class NoteTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.NOTE}

    if TYPE_CHECKING:
        def __init__(self, *, mod: TicketMod, targetid: int, id: int = ..., stage: TicketStage = ...,
            status: TicketStatus = ..., auditid: Optional[int] = ..., duration: Optional[int] = ...,
            comment: Optional[str] = ..., list_msgid: Optional[int] = ..., delivered_id: Optional[int] = ...,
            created_at: datetime = ..., modified_by: Optional[int] = ...) -> None: ...

    can_revert = True

    trigger_action = None
    revert_trigger_action = None

    colors = 0xFFFFFF, 0xFFFFFF, 0x666666

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        return "{}**Note**{}".format(
            format("{!m} added ", self.modid) if mod else "", format(" for {!m}", self.targetid) if target else "")

    async def revert_action(self, reason: Optional[str] = None) -> None:
        pass

    async def revert(self, actorid: int) -> None:
        self.status = TicketStatus.HIDDEN
        self.modified_by = actorid

    async def expire(self) -> None:
        self.status = TicketStatus.HIDDEN
        self.modified_by = None

blame_re: re.Pattern[str] = re.compile(r"^[^:]* by (\d+): (.*)$", re.I)

async def audit_ticket_data(session: AsyncSession, audit: AuditLogEntry, *,
    need_duration: bool = True, can_have_duration: bool = True) -> Dict[str, Any]:
    assert isinstance(audit.target, (User, Member))
    assert audit.user is not None
    mod_id = audit.user.id
    stage = TicketStage.NEW
    duration = None
    have_duration = False
    if audit.user.bot:
        if audit.reason is None:
            comment = "No reason attached to the audit log"
            stage = TicketStage.COMMENTED
        elif (match := blame_re.match(audit.reason)) is not None:
            mod_id = int(match.group(1))
            duration, have_duration, comment = TicketMod.parse_duration_comment(match.group(2))
        else:
            comment = audit.reason
            stage = TicketStage.COMMENTED
    elif audit.reason is None:
        comment = None
    else:
        duration, have_duration, comment = TicketMod.parse_duration_comment(audit.reason)
    if comment and (have_duration or not need_duration):
        stage = TicketStage.COMMENTED
    return {
        "mod": await TicketMod.get(session, mod_id),
        "modid": mod_id,
        "targetid": audit.target.id,
        "auditid": audit.id,
        "created_at": audit.created_at.replace(tzinfo=None),
        "comment": comment,
        "stage": stage,
        "duration": duration if can_have_duration else None}

@registry.mapped
@register_action
class KickTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.KICK}

    if TYPE_CHECKING:
        def __init__(self, *, mod: TicketMod, targetid: int, id: int = ..., stage: TicketStage = ...,
            status: TicketStatus = ..., auditid: Optional[int] = ..., duration: Optional[int] = ...,
            comment: Optional[str] = ..., list_msgid: Optional[int] = ..., delivered_id: Optional[int] = ...,
            created_at: datetime = ..., modified_by: Optional[int] = ...) -> None: ...

    can_revert = False

    trigger_action = AuditLogAction.kick
    revert_trigger_action = None

    colors = 0xFFBB55, 0xFF9900, 0x995500

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        return "{}**Kicked**{}".format(
            format("{!m} ", self.modid) if mod else "", format(" {!m}", self.targetid) if target else "")

    @staticmethod
    async def create_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        return (KickTicket(**await audit_ticket_data(session, audit, can_have_duration=False)),)

    async def hide(self, actorid: int, reason: Optional[str] = None) -> None:
        logger.debug("Hiding Ticket #{}".format(self.id))
        self.status = TicketStatus.HIDDEN
        self.modified_by = actorid
        if reason is not None:
            self.append_comment(reason)

@registry.mapped
@register_action
class BanTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.BAN}

    if TYPE_CHECKING:
        def __init__(self, *, mod: TicketMod, targetid: int, id: int = ..., stage: TicketStage = ...,
            status: TicketStatus = ..., auditid: Optional[int] = ..., duration: Optional[int] = ...,
            comment: Optional[str] = ..., list_msgid: Optional[int] = ..., delivered_id: Optional[int] = ...,
            created_at: datetime = ..., modified_by: Optional[int] = ...) -> None: ...

    can_revert = True

    trigger_action = AuditLogAction.ban
    revert_trigger_action = AuditLogAction.unban

    colors = 0xFF6666, 0xFF0000, 0x990000

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        return "{}**Banned**{}".format(
            format("{!m} ", self.modid) if mod else "", format(" {!m}", self.targetid) if target else "")

    @staticmethod
    async def create_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        return (BanTicket(**await audit_ticket_data(session, audit)),)

    @staticmethod
    async def revert_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        stmt = select(BanTicket).where(
            BanTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
            BanTicket.targetid == audit.target.id)
        return (await session.execute(stmt)).scalars().all()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = client.get_guild(conf.guild)
        assert guild
        async for entry in guild.bans(limit=None): # TODO: before/after?
            if entry.user.id == self.targetid:
                await guild.unban(entry.user, reason=reason)
                break

@registry.mapped
@register_action
class VCMuteTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.VC_MUTE}

    if TYPE_CHECKING:
        def __init__(self, *, mod: TicketMod, targetid: int, id: int = ..., stage: TicketStage = ...,
            status: TicketStatus = ..., auditid: Optional[int] = ..., duration: Optional[int] = ...,
            comment: Optional[str] = ..., list_msgid: Optional[int] = ..., delivered_id: Optional[int] = ...,
            created_at: datetime = ..., modified_by: Optional[int] = ...) -> None: ...

    can_revert = True

    trigger_action = AuditLogAction.member_update
    revert_trigger_action = AuditLogAction.member_update

    colors = 0xFF55BB, 0xFF0099, 0x990055

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        return "{}**VC Muted**{}".format(
            format("{!m} ", self.modid) if mod else "", format(" {!m}", self.targetid) if target else "")

    @staticmethod
    async def create_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        if not getattr(audit.before, "mute", True) and getattr(audit.after, "mute", False):
            return (VCMuteTicket(**await audit_ticket_data(session, audit)),)
        else:
            return ()

    @staticmethod
    async def revert_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        if getattr(audit.before, "mute", False) and not getattr(audit.after, "mute", False):
            stmt = select(VCMuteTicket).where(
                VCMuteTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                VCMuteTicket.targetid == audit.target.id)
            return (await session.execute(stmt)).scalars().all()
        else:
            return ()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = client.get_guild(conf.guild)
        assert guild
        try:
            member = await guild.fetch_member(self.targetid)
        except discord.NotFound:
            # User is no longer in the guild, nothing to do
            return
        try:
            await member.edit(mute=False, reason=reason)
        except discord.HTTPException as exc:
            if exc.text != "Target user is not connected to voice.":
                raise
            conf.pending_unmutes = conf.pending_unmutes + [self.targetid]
            await conf
            logger.debug("Pending unmute for {}".format(self.targetid))

@registry.mapped
@register_action
class VCDeafenTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.VC_DEAFEN}

    if TYPE_CHECKING:
        def __init__(self, *, mod: TicketMod, targetid: int, id: int = ..., stage: TicketStage = ...,
            status: TicketStatus = ..., auditid: Optional[int] = ..., duration: Optional[int] = ...,
            comment: Optional[str] = ..., list_msgid: Optional[int] = ..., delivered_id: Optional[int] = ...,
            created_at: datetime = ..., modified_by: Optional[int] = ...) -> None: ...

    can_revert = True

    trigger_action = AuditLogAction.member_update
    revert_trigger_action = AuditLogAction.member_update

    colors = 0xCC66FF, 0x9900FF, 0x550099

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        return "{}**VC Deafened**{}".format(
            format("{!m} ", self.modid) if mod else "", format(" {!m}", self.targetid) if target else "")

    @staticmethod
    async def create_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        if not getattr(audit.before, "deaf", True) and getattr(audit.after, "deaf", False):
            return (VCDeafenTicket(**await audit_ticket_data(session, audit)),)
        else:
            return ()

    @staticmethod
    async def revert_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        if getattr(audit.before, "deaf", False) and not getattr(audit.after, "deaf", False):
            stmt = select(VCDeafenTicket).where(
                VCDeafenTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                VCDeafenTicket.targetid == audit.target.id)
            return (await session.execute(stmt)).scalars().all()
        else:
            return ()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = client.get_guild(conf.guild)
        assert guild
        try:
            member = await guild.fetch_member(self.targetid)
        except discord.NotFound:
            # User is no longer in the guild, nothing to do
            return
        try:
            await member.edit(deafen=False, reason=reason)
        except discord.HTTPException as exc:
            if exc.text != "Target user is not connected to voice.":
                raise
            conf.pending_undeafens = conf.pending_undeafens + [self.targetid]
            await conf
            logger.debug("Pending undeafen for {}".format(self.targetid))

@registry.mapped
@register_action
class AddRoleTicket(Ticket):
    roleid: Mapped[int] = mapped_column(BigInteger, nullable=True)
    __mapper_args__ = {"polymorphic_identity": TicketType.ADD_ROLE, "polymorphic_load": "inline"}

    if TYPE_CHECKING:
        def __init__(self, *, mod: TicketMod, targetid: int, roleid: int, id: int = ..., stage: TicketStage = ...,
            status: TicketStatus = ..., auditid: Optional[int] = ..., duration: Optional[int] = ...,
            comment: Optional[str] = ..., list_msgid: Optional[int] = ..., delivered_id: Optional[int] = ...,
            created_at: datetime = ..., modified_by: Optional[int] = ...) -> None: ...

    can_revert = True

    trigger_action = AuditLogAction.member_role_update
    revert_trigger_action = AuditLogAction.member_role_update

    colors = 0xFFFF99, 0xFFFF00, 0x999900

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        role_desc = format("{!M}", self.roleid)
        if dm:
            if (guild := client.get_guild(conf.guild)) and (role := guild.get_role(self.roleid)):
                role_desc = role.name
            else:
                role_desc = str(self.roleid)
        return "{}**Role {}**{}".format(format("{!m} added ", self.modid) if mod else "", role_desc,
            format(" to {!m}", self.targetid) if target else "")

    @staticmethod
    async def create_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        tickets = []
        for role in audit.changes.after.roles or ():
            if role.id in conf.tracked_roles:
                tickets.append(AddRoleTicket(roleid=role.id, **await audit_ticket_data(session, audit)))
        return tickets

    @staticmethod
    async def revert_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        tickets: List[Ticket] = []
        for role in audit.changes.before.roles or ():
            if role.id in conf.tracked_roles:
                stmt = select(AddRoleTicket).where(
                    AddRoleTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                    AddRoleTicket.targetid == audit.target.id, AddRoleTicket.roleid == role.id)
                tickets.extend((await session.execute(stmt)).scalars())
        return tickets

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = client.get_guild(conf.guild)
        assert guild
        role = guild.get_role(self.roleid)
        assert role
        try:
            member = await guild.fetch_member(self.targetid)
        except discord.NotFound:
            await plugins.persistence.drop_persistent_role(user_id=self.targetid, role_id=self.roleid)
            return
        await member.remove_roles(role, reason=reason)

@registry.mapped
@register_action
class TimeoutTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.TIMEOUT, "polymorphic_load": "inline"}

    if TYPE_CHECKING:
        def __init__(self, *, mod: TicketMod, targetid: int, id: int = ..., stage: TicketStage = ...,
            status: TicketStatus = ..., auditid: Optional[int] = ..., duration: Optional[int] = ...,
            comment: Optional[str] = ..., list_msgid: Optional[int] = ..., delivered_id: Optional[int] = ...,
            created_at: datetime = ..., modified_by: Optional[int] = ...) -> None: ...

    can_revert = True

    trigger_action = AuditLogAction.member_update
    update_trigger_action = AuditLogAction.member_update
    revert_trigger_action = AuditLogAction.member_update

    colors = 0xBBFF55, 0x99FF00, 0x559900

    def describe(self, *, mod: bool = True, target: bool = True, dm: bool = False) -> str:
        return "{}**Timed out**{}".format(
            format("{!m} ", self.modid) if mod else "", format(" {!m}", self.targetid) if target else "")

    async def set_duration(self, duration: Optional[int]) -> None:
        if duration is not None:
            guild = client.get_guild(conf.guild)
            assert guild
            try:
                member = await guild.fetch_member(self.targetid)
                await member.edit(
                    timed_out_until=min(
                        self.created_at.replace(tzinfo=timezone.utc) + timedelta(seconds=duration),
                        discord.utils.utcnow() + timedelta(days=28)),
                    reason="Synchronization with ticket")
                self.duration = duration
            except (discord.NotFound, discord.Forbidden):
                pass

    @staticmethod
    async def create_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        old = getattr(audit.changes.before, "timed_out_until", None)
        new = getattr(audit.changes.after, "timed_out_until", None)
        if new is not None and (old is None or old < new) and audit.reason != "Synchronization with ticket":
            ticket = TimeoutTicket(**await audit_ticket_data(session, audit, need_duration=False))
            if ticket.duration is None:
                ticket.duration = (audit.changes.after.timed_out_until - discord.utils.utcnow()).total_seconds()
            else:
                await ticket.set_duration(ticket.duration)
            return (ticket,)
        return ()

    @staticmethod
    async def update_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        now = discord.utils.utcnow()
        old = getattr(audit.changes.before, "timed_out_until", None)
        new = getattr(audit.changes.after, "timed_out_until", None)
        if (old is not None and new is not None and old > now and old != new
            and audit.reason != "Synchronization with ticket"):
            stmt = select(TimeoutTicket).where(
                AddRoleTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                AddRoleTicket.targetid == audit.target.id)
            tickets = (await session.execute(stmt)).scalars().all()
            for ticket in tickets:
                ticket.duration = (audit.changes.after.timed_out_until
                    - ticket.created_at.replace(tzinfo=timezone.utc)).total_seconds()
            return tickets
        return ()

    @staticmethod
    async def revert_from_audit(session: AsyncSession, audit: AuditLogEntry) -> Sequence[Ticket]:
        assert isinstance(audit.target, (User, Member))
        assert audit.user is not None
        old = getattr(audit.changes.before, "timed_out_until", None)
        new = getattr(audit.changes.after, "timed_out_until", None)
        if old is not None and new is None:
            stmt = select(TimeoutTicket).where(
                AddRoleTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                AddRoleTicket.targetid == audit.target.id)
            return (await session.execute(stmt)).scalars().all()
        else:
            return ()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        # Discord should lift the timeout on its own
        pass

@plugins.init
async def init_db() -> None:
    await util.db.init(util.db.get_ddl(
        CreateSchema("tickets"),
        registry.metadata.create_all,
        DDL(r"""
            CREATE INDEX tickets_mod_queue ON tickets.tickets USING BTREE (modid, id) WHERE stage <> 'COMMENTED';

            CREATE VIEW tickets.mod_queues AS
                SELECT tkt.id AS id
                    FROM tickets.mods mod
                        INNER JOIN tickets.tickets tkt ON mod.modid = tkt.modid AND tkt.id =
                            (SELECT t.id
                                FROM tickets.tickets t
                                WHERE mod.modid = t.modid AND stage <> 'COMMENTED'
                                ORDER BY t.id LIMIT 1
                            );

            CREATE FUNCTION tickets.log_ticket_update()
            RETURNS TRIGGER AS $log_ticket_update$
                DECLARE
                    last_version INT;
                BEGIN
                    SELECT version INTO last_version
                        FROM tickets.history
                        WHERE id = OLD.id
                        ORDER BY version DESC LIMIT 1;
                    IF NOT FOUND THEN
                        INSERT INTO tickets.history
                            VALUES
                                ( 0
                                , OLD.created_at
                                , OLD.id
                                , OLD.type
                                , OLD.stage
                                , OLD.status
                                , OLD.modid
                                , OLD.targetid
                                , OLD.roleid
                                , OLD.auditid
                                , OLD.duration
                                , OLD.comment
                                , OLD.list_msgid
                                , OLD.delivered_id
                                , OLD.created_at
                                , OLD.modified_by
                                );
                        last_version = 0;
                    END IF;
                    INSERT INTO tickets.history
                        VALUES
                            ( last_version + 1
                            , CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                            , NEW.id
                            , NULLIF(NEW.type, OLD.type)
                            , NULLIF(NEW.stage, OLD.stage)
                            , NULLIF(NEW.status, OLD.status)
                            , NULLIF(NEW.modid, OLD.modid)
                            , NULLIF(NEW.targetid, OLD.targetid)
                            , NULLIF(NEW.roleid, OLD.roleid)
                            , NULLIF(NEW.auditid, OLD.auditid)
                            , NULLIF(NEW.duration, OLD.duration)
                            , NULLIF(NEW.comment, OLD.comment)
                            , NULLIF(NEW.list_msgid, OLD.list_msgid)
                            , NULLIF(NEW.delivered_id, OLD.delivered_id)
                            , NULLIF(NEW.created_at, OLD.created_at)
                            , NEW.modified_by
                            );
                    RETURN NULL;
                END
            $log_ticket_update$ LANGUAGE plpgsql;

            CREATE TRIGGER log_update
                AFTER UPDATE ON
                    tickets.tickets
                FOR EACH ROW
                WHEN
                    (OLD.* IS DISTINCT FROM NEW.*)
                EXECUTE PROCEDURE
                    tickets.log_ticket_update();
        """)))

# ----------- Audit logs -----------
audit_log_event = asyncio.Event()

def audit_log_updated() -> None:
    audit_log_event.set()

async def poll_audit_log() -> None:
    """
    Whenever this task is woken up via audit_log_updated, it will read any new audit log events and process them.
    """
    await client.wait_until_ready()
    if not conf.guild or not (guild := client.get_guild(conf.guild)):
        logger.error("Guild not configured, or can't find the configured guild! Cannot read audit log.")
        return

    last = conf.last_auditid
    while True:
        try:
            try:
                await asyncio.wait_for(audit_log_event.wait(), timeout=600)
                while True:
                    audit_log_event.clear()
                    await asyncio.wait_for(audit_log_event.wait(), timeout=conf.audit_log_precision)
            except asyncio.TimeoutError:
                pass

            logger.debug("Reading audit entries since {}".format(last))
            # audit_logs(after) is currently broken so we read the entire audit
            # log in reverse chronological order and reverse it
            entries = []
            async for entry in guild.audit_logs(limit=None if last else 1, oldest_first=False):
                if last and entry.id <= last:
                    break
                entries.append(entry)
            async with sessionmaker() as session:
                for entry in reversed(entries):
                    try:
                        logger.debug("Processing audit entry {}".format(entry))
                        last = entry.id
                        for create_handler in create_handlers.get(entry.action, ()):
                            for ticket in await create_handler(session, entry):
                                session.add(ticket)
                                logger.debug("Created {!r} from audit {}".format(ticket.describe(), entry.id))
                                await session.commit() # to get ID
                                await ticket.publish()
                        for update_handler in update_handlers.get(entry.action, ()):
                            for ticket in await update_handler(session, entry):
                                if entry.user is not None:
                                    ticket.modified_by = entry.user.id
                                logger.debug("Updated Ticket #{} from audit {}".format(ticket.id, entry.id))
                                await ticket.publish()
                        for revert_handler in revert_handlers.get(entry.action, ()):
                            for ticket in await revert_handler(session, entry):
                                ticket.status = TicketStatus.REVERTED
                                if entry.user is not None:
                                    ticket.modified_by = entry.user.id
                                logger.debug("Reverted Ticket #{} from audit {}".format(ticket.id, entry.id))
                                await ticket.publish()
                    except asyncio.CancelledError:
                        raise
                    except:
                        logger.error("Processing audit entry {}".format(entry), exc_info=True)
                await session.commit()

        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in audit log task", exc_info=True)
            await asyncio.sleep(60)
        finally:
            conf.last_auditid = last
            await conf

audit_log_task: asyncio.Task[None]

# ----------- Ticket expiry system -----------
expiration_event = asyncio.Event()

def expiry_updated() -> None:
    expiration_event.set()

# TODO: scheduling module
async def expire_tickets() -> None:
    await client.wait_until_ready()

    while True:
        try:
            async with sessionmaker() as session:
                min_expiry = None
                now = datetime.utcnow()
                stmt = select(Ticket).where(Ticket.status == TicketStatus.IN_EFFECT, Ticket.duration != None)
                for ticket, in await session.execute(stmt):
                    if (expiry := ticket.expiry) is None:
                        continue
                    if expiry <= now:
                        try:
                            await ticket.expire()
                        except asyncio.CancelledError:
                            raise
                        except:
                            logger.error("Exception when expiring Ticket #{}".format(ticket.id), exc_info=True)
                    elif min_expiry is None or expiry < min_expiry:
                        min_expiry = expiry
                async with Ticket.publish_all(session):
                    await session.commit()

            delay = (min_expiry - datetime.utcnow()).total_seconds() if min_expiry is not None else 86400.0
            logger.debug("Waiting for upcoming expiration in {} seconds".format(delay))
            try:
                await asyncio.wait_for(expiration_event.wait(), timeout=delay)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass
            expiration_event.clear()
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in ticket expiry task", exc_info=True)
            await asyncio.sleep(60)

expiry_task: asyncio.Task[None]

# ----------- Ticket delivery system  -----------
queued_mods: Set[int] = set()

delivery_event = asyncio.Event()

def delivery_updated() -> None:
    delivery_event.set()

async def deliver_tickets() -> None:
    global queued_mods
    await client.wait_until_ready()

    while True:
        try:
            async with sessionmaker() as session:
                stmt = select(TicketMod).options(joinedload(TicketMod.queue_top)).where(TicketMod.queue_top != None)
                mods = (await session.execute(stmt)).scalars().all()

                queued_mods = set(mod.modid for mod in mods)
                logger.debug("Listening for comments from {!r}".format(queued_mods))

                min_delivery = None
                now = datetime.utcnow()
                for mod in mods:
                    if (ticket := mod.queue_top) is None:
                        continue
                    if mod.scheduled_delivery is None or mod.scheduled_delivery <= now:
                        try:
                            related = await ticket.get_related(session)
                            if ticket.stage == TicketStage.NEW:
                                await mod.try_initial_delivery(ticket, related)
                            else:
                                await mod.try_redelivery(ticket, related)
                        except asyncio.CancelledError:
                            raise
                        except:
                            logger.error(format("Exception when delivering a ticket to {!m}", mod.modid),
                                exc_info=True)
                    if mod.scheduled_delivery is not None:
                        if min_delivery is None or mod.scheduled_delivery < min_delivery:
                            min_delivery = mod.scheduled_delivery
                # Can't have any publishable changes
                await session.commit()

            delay = (min_delivery - datetime.utcnow()).total_seconds() if min_delivery is not None else 86400.0
            logger.debug("Waiting for upcoming delivery in {} seconds".format(delay))
            try:
                await asyncio.wait_for(delivery_event.wait(), timeout=delay)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass
            delivery_event.clear()
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in ticket delivery task", exc_info=True)
            await asyncio.sleep(60)

delivery_task: asyncio.Task[None]

@plugins.init
async def init_tasks() -> None:
    global audit_log_task, expiry_task, delivery_task
    audit_log_task = asyncio.create_task(poll_audit_log())
    plugins.finalizer(audit_log_task.cancel)
    expiry_task = asyncio.create_task(expire_tickets())
    plugins.finalizer(expiry_task.cancel)
    delivery_task = asyncio.create_task(deliver_tickets())
    plugins.finalizer(delivery_task.cancel)
    audit_log_updated()
    expiry_updated()
    delivery_updated()


# ------------ Commands ------------

async def resolve_ticket(ref: Optional[MessageReference], ticket_arg: Optional[Union[PartialMessage, int]],
    session: AsyncSession) -> Ticket:
    """
    Resolves a ticket from the given message and command arg, if possible.
    """
    if isinstance(ticket_arg, int):
        # This is either a message snowflake (a big number) or a ticket id (small number). The leading 42 bits of a
        # snowflake are the timestamp and we assume that if all of those are zero, it's probably not a snowflake as
        # that would imply an epoch time of 0 milliseconds.
        if ticket_arg < 1 << 22:
            ticket = await session.get(Ticket, ticket_arg)
            if ticket is None:
                raise InvocationError("No ticket with ID {}".format(ticket_arg))
            return ticket
        else:
            stmt = select(Ticket).where(Ticket.list_msgid == ticket_arg)
            ticket = (await session.execute(stmt)).scalar()
            if ticket is None:
                raise InvocationError("Message ID {} is not referring to a ticket".format(ticket_arg))
            return ticket
    elif isinstance(ticket_arg, PartialMessage):
        stmt = select(Ticket).where(Ticket.list_msgid == ticket_arg.id)
        ticket = (await session.execute(stmt)).scalar()
        if ticket is None:
            raise InvocationError("Message ID {} is not referring to a ticket".format(ticket_arg.id))
        return ticket
    elif ref is not None:
        stmt = select(Ticket).where(Ticket.list_msgid == ref.message_id)
        ticket = (await session.execute(stmt)).scalar()
        if ticket is None:
            raise InvocationError("Message ID {} is not referring to a ticket".format(ref.message_id))
        return ticket
    else:
        raise InvocationError("Specify a ticket by ID, message ID, or by replying to it")

def summarise_tickets(tickets: Sequence[Ticket], title: str, *, mod: bool = True, target: bool = True, dm: bool = False
    ) -> Optional[Iterator[Embed]]:
    """
    Create paged embeds of ticket summaries from the provided list of tickets.
    """
    if not tickets:
        return None

    lines = ["[#{}]({}): {}".format(ticket.id, ticket.jump_link, ticket.describe(mod=mod, target=target, dm=dm))
        for ticket in tickets]
    blocks = ['\n'.join(lines[i:i+10]) for i in range(0, len(lines), 10)]
    page_count = len(blocks)

    embeds = (Embed(description=blocks[i], title=title) for i in range(page_count))
    if page_count > 1:
        embeds = (embed.set_footer(text="Page {}/{}".format(i+1, page_count)) for i, embed in enumerate(embeds))
    return embeds

class Page(NamedTuple):
    content: Optional[str] = None
    embed: Optional[Embed] = None

async def pager(dest: Messageable, pages: List[Page]) -> None:
    """
    Page a sequence of pages.
    """
    next_reaction = '\u23ED'
    prev_reaction = '\u23EE'
    all_reaction = '\U0001F4DC'
    reactions = (prev_reaction, all_reaction, next_reaction)

    pages = list(pages)

    # Sanity check
    if not pages:
        raise ValueError("Cannot page with no pages!")

    # Send first page
    msg = await dest.send(allowed_mentions=AllowedMentions.none(), **pages[0]._asdict())

    if len(pages) == 1:
        return

    # Add reactions
    for r in reactions:
        await msg.add_reaction(r)

    bot_id = client.user.id if client.user is not None else None
    index = 0
    with ReactionMonitor(channel_id=msg.channel.id, message_id=msg.id, event="add",
        filter=lambda _, p: p.user_id != bot_id and p.emoji.name in reactions,
        timeout_each=120) as mon:
        try:
            while True:
                _, payload = await mon
                if str(payload.emoji) == next_reaction:
                    index += 1
                elif str(payload.emoji) == prev_reaction:
                    index -= 1
                elif str(payload.emoji) == all_reaction:
                    await msg.delete()
                    for page in pages:
                        await dest.send(allowed_mentions=AllowedMentions.none(), **page._asdict())
                    break
                index %= len(pages)
                await msg.edit(allowed_mentions=AllowedMentions.none(), **pages[index]._asdict())
                try:
                    await msg.remove_reaction(payload.emoji, Object(payload.user_id))
                except discord.HTTPException:
                    pass
            else:
                # Remove the reactions
                try:
                    for r in reactions:
                        await msg.clear_reaction(r)
                except discord.HTTPException:
                    pass
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            pass

voice_lock: asyncio.Lock = asyncio.Lock()

@cog
class Tickets(Cog):
    """Manage infraction history"""
    @cleanup
    @command("note")
    @priv("mod")
    async def note_command(self, ctx: Context, target: PartialUserConverter, *, note: Optional[str]) -> None:
        """Create a note on the target user."""
        if note is None:
            # Request the note dynamically
            prompt = await ctx.send("Please enter the note:")
            response = await get_input(prompt, ctx.author, {"\u274C": None}, timeout=300)
            if response is not None:
                note = response.content

        if note is not None:
            async with sessionmaker() as session:
                ticket = await create_note(session, note, modid=ctx.author.id, targetid=target.id)
                async with Ticket.publish_all(session):
                    await session.commit()
                await session.commit()
            await ctx.send(embed=Embed(
                description="[#{}]({}): Note created!".format(ticket.id, ticket.jump_link)))

    @group("ticket", aliases=["tickets"])
    @priv("mod")
    async def ticket_command(self, ctx: Context) -> None:
        """Manage tickets. A ticket can be specified by ID, by Discord message link, or by replying to it."""
        pass

    @cleanup
    @ticket_command.command("top")
    async def ticket_top(self, ctx: Context) -> None:
        """Re-deliver the ticket at the top of your queue to your DMs."""
        async with sessionmaker() as session:
            mod = await session.get(TicketMod, ctx.author.id,
                options=(joinedload(TicketMod.queue_top),))

            if mod is None or mod.queue_top is None:
                await ctx.send("Your queue is empty, good job!")
            else:
                ticket = mod.queue_top
                await mod.try_redelivery(ticket, await ticket.get_related(session))
                if ctx.channel.type != ChannelType.private:
                    await ctx.send("Ticket #{} has been delivered to your DMs.".format(mod.queue_top.id))

            await session.commit()

    @cleanup
    @ticket_command.command("queue")
    async def ticket_queue(self, ctx: Context, mod: Optional[PartialUserConverter]) -> None:
        """Show the specified moderator's (or your own) ticket queue."""
        user = ctx.author if mod is None else mod

        async with sessionmaker() as session:
            stmt = select(Ticket).where(Ticket.modid == user.id, Ticket.stage != TicketStage.COMMENTED
                ).order_by(Ticket.id)
            tickets = (await session.execute(stmt)).scalars().all()
            embeds = summarise_tickets(tickets, "Queue for {}".format(user), mod=False,
                dm=ctx.channel.type == ChannelType.private)

        if embeds:
            await pager(ctx, [Page(embed=embed) for embed in embeds])
        else:
            await ctx.send(format("{!m} has an empty queue!", user),
                allowed_mentions=AllowedMentions.none())

    @ticket_command.command("take")
    async def ticket_take(self, ctx: Context, ticket: Optional[Union[PartialMessage, int]]) -> None:
        """Assign the specified ticket to yourself."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if tkt.modid == ctx.author.id:
                await ctx.send("This is already your ticket!")
            else:
                await tkt.mod.transfer(tkt, ctx.author.id, actorid=ctx.author.id)
                await tkt.publish()
                await session.commit()

                await ctx.send("You have claimed Ticket #{}.".format(tkt.id))

    @ticket_command.command("assign")
    async def ticket_assign(self, ctx: Context, ticket: Optional[Union[PartialMessage, int]], mod: PartialUserConverter
        ) -> None:
        """Assign the specified ticket to the specified moderator."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if mod.id == tkt.modid:
                await ctx.send(format("Ticket #{} is already assigned to {!m}", tkt.id, mod.id),
                    allowed_mentions=AllowedMentions.none())
            else:
                await tkt.mod.transfer(tkt, mod.id, actorid=ctx.author.id)
                await tkt.publish()
                await session.commit()

                await ctx.send(format("Assigned Ticket #{} to {!m}", tkt.id, mod.id),
                    allowed_mentions=AllowedMentions.none())

    @ticket_command.command("set")
    async def ticket_set(self, ctx: Context, ticket: Optional[Union[PartialMessage, int]], *, duration_comment: str
        ) -> None:
        """Set the duration and comment for a ticket."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)

            duration, have_duration, comment = TicketMod.parse_duration_comment(duration_comment)
            duration, have_duration, message = tkt.duration_message(duration, have_duration)
            if have_duration:
                await tkt.set_duration(duration)
            if comment:
                tkt.comment = comment
            tkt.modified_by = ctx.author.id

            if tkt.stage != TicketStage.COMMENTED and tkt.modid == ctx.author.id and have_duration and comment:
                mod = tkt.mod
                if tkt == await mod.load_queue():
                    tkt.stage = TicketStage.COMMENTED
                    await mod.update_delivered_message(tkt)
                    mod.scheduled_delivery = None
                else:
                    tkt.stage = TicketStage.COMMENTED
                delivery_updated()

            await tkt.publish()
            await session.commit()

            await ctx.send(embed=Embed(description="[#{}]({}): Ticket updated. {}".format(
                tkt.id, tkt.jump_link, message)))

    @ticket_command.command("append")
    async def ticket_append(self, ctx: Context, ticket: Optional[Union[PartialMessage, int]], *, comment: str) -> None:
        """Append to a ticket's comment."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if len(tkt.comment or "") + len(comment) > 2000:
                raise UserError("Cannot append, exceeds maximum comment length!")

            tkt.append_comment(comment)
            tkt.modified_by = ctx.author.id
            await tkt.publish()
            await session.commit()

            await ctx.send(embed=Embed(description="[#{}]({}): Ticket updated.".format(
                tkt.id, tkt.jump_link)))

    @ticket_command.command("revert")
    async def ticket_revert(self, ctx: Context, ticket: Optional[Union[PartialMessage, int]]) -> None:
        """Manually revert a ticket."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if not tkt.can_revert:
                raise UserError("This ticket type ({}) cannot be reverted!".format(tkt.type.value))
            if not tkt.status in (TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED):
                await ctx.send(embed=Embed(
                    description=("[#{}]({}): Cannot be reverted as it is no longer active!".format(
                        tkt.id, tkt.jump_link))))
                return

            await tkt.revert(ctx.author.id)
            await tkt.publish()
            await session.commit()

            await ctx.send(embed=Embed(
                description="[#{}]({}): Ticket reverted.".format(tkt.id, tkt.jump_link)))

    @ticket_command.command("hide")
    async def ticket_hide(self, ctx: Context, ticket: Optional[Union[PartialMessage, int]], *, comment: Optional[str]
        ) -> None:
        """Hide (and revert) a ticket."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if tkt.hidden:
                await ctx.send(embed=Embed(description="#{}: Is already hidden!".format(tkt.id)))
                return

            await tkt.hide(ctx.author.id, reason=comment)
            await tkt.publish()
            await session.commit()

            await ctx.send(embed=Embed(description="#{}: Ticket hidden.".format(tkt.id)))

    @cleanup
    @ticket_command.command("show", usage="<user | ticket>")
    async def ticket_show(self, ctx: Context, *, user_or_id: Union[PartialUserConverter, PartialMessage, int]) -> None:
        """Show tickets affecting given user, or a ticket with a specific ID."""
        async with sessionmaker() as session:
            if isinstance(user_or_id, (PartialMessage, int)):
                tkt = await resolve_ticket(None, user_or_id, session)
                await ctx.send(embed=tkt.to_embed(dm=ctx.channel.type == ChannelType.private))
            else:
                stmt = select(Ticket).where(Ticket.targetid == user_or_id.id).order_by(Ticket.id)
                tickets = (await session.execute(stmt)).scalars().all()

                shown = []
                hidden = []
                for tkt in tickets:
                    if tkt.status == TicketStatus.HIDDEN:
                        hidden.append(tkt)
                    else:
                        shown.append(tkt)

                embeds: Optional[Iterable[Embed]] = summarise_tickets(shown,
                    title='Tickets for {}'.format(user_or_id.id), target=False,
                    dm=ctx.channel.type == ChannelType.private)
                hidden_field = ', '.join('#{}'.format(tkt.id) for tkt in hidden)

                if hidden_field:
                    embeds = embeds or (Embed(title='Tickets for {}'.format(user_or_id.id)),)
                    embeds = (embed.add_field(name="Hidden", value=hidden_field) for embed in embeds)

                if embeds:
                    await pager(ctx, [Page(embed=embed) for embed in embeds])
                else:
                    await ctx.send("No tickets found for this user.")

    @cleanup
    @ticket_command.command("showhidden", usage="<user | ticket>")
    async def ticket_showhidden(self, ctx: Context, *, user_or_id: Union[PartialUserConverter, PartialMessage, int]
        ) -> None:
        """Show hidden tickets affecting given user, or a ticket with a specific ID."""
        async with sessionmaker() as session:
            if isinstance(user_or_id, (PartialMessage, int)):
                tkt = await resolve_ticket(None, user_or_id, session)
                await ctx.send(embed=tkt.to_embed(dm=ctx.channel.type == ChannelType.private))
            else:
                stmt = select(Ticket).where(
                    Ticket.status == TicketStatus.HIDDEN, Ticket.targetid == user_or_id.id).order_by(Ticket.id)
                tickets = (await session.execute(stmt)).scalars().all()

                embeds = summarise_tickets(tickets, title='Hidden tickets for {}'.format(user_or_id.id),
                    target=False, dm=ctx.channel.type == ChannelType.private)

                if embeds:
                    await pager(ctx, [Page(embed=embed) for embed in embeds])
                else:
                    await ctx.send("No hidden tickets found for this user.")

    @cleanup
    @ticket_command.command("history")
    async def ticket_history(self, ctx: Context, *, ticket: Optional[Union[PartialMessage, int]]) -> None:
        """Show revision history for a ticket."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)

            items = []
            for history in await TicketHistory.get(session, tkt.id):
                row = []
                if history.last_modified_at is not None:
                    timestamp = int(history.last_modified_at.replace(tzinfo=timezone.utc).timestamp())
                    row.append("<t:{}:f>, <t:{}:R>".format(timestamp, timestamp))
                if history.modified_by is not None:
                    row.append(format("by {!m}", history.modified_by))
                if history.type is not None:
                    row.append(history.type.value)
                if history.stage is not None:
                    row.append(history.stage.value)
                if history.status is not None:
                    row.append(history.status.value)
                if history.modid is not None:
                    row.append(format("moderator: {!m}", history.modid))
                if history.targetid is not None:
                    row.append(format("target: {!m}", history.targetid))
                if history.roleid is not None:
                    row.append(format("role: {!M}", history.roleid))
                if history.duration is not None:
                    row.append(str(timedelta(seconds=history.duration)))
                if history.comment is not None:
                    row.append(format("comment: {!i}", history.comment))
                if history.list_msgid is not None:
                    row.append("https://discord.com/channels/{}/{}/{}".format(
                        conf.guild, conf.ticket_list, history.list_msgid))
                if history.auditid is not None:
                    row.append("from audit {}".format(history.auditid))
                items.append(PlainItem(", ".join(row) + "\n"))
            return await pager(ctx, [Page(content=content) for content, _ in chunk_messages(items)])

    @Cog.listener("on_member_ban")
    @Cog.listener("on_member_unban")
    @Cog.listener("on_member_remove")
    async def on_member_remove(self, *args: Any) -> None:
        audit_log_updated()


    @Cog.listener("on_voice_state_update")
    async def process_voice_state(self, member: Member, before: VoiceState, after: VoiceState) -> None:
        if before.deaf != after.deaf or before.mute != after.mute:
            audit_log_updated()
        if after.channel is not None:
            async with voice_lock:
                if member.id in conf.pending_unmutes:
                    try:
                        await member.edit(mute=False)
                        conf.pending_unmutes = FrozenList(
                            filter(lambda i: i != member.id, conf.pending_unmutes))
                        logger.debug("Processed unmute for {}".format(member.id))
                        await conf
                    except discord.HTTPException as exc:
                        if exc.text != "Target user is not connected to voice.":
                            raise
                if member.id in conf.pending_undeafens:
                    try:
                        await member.edit(deafen=False)
                        conf.pending_undeafens = FrozenList(
                            filter(lambda i: i != member.id, conf.pending_undeafens))
                        logger.debug("Processed undeafen for {}".format(member.id))
                        await conf
                    except discord.HTTPException as exc:
                        if exc.text != "Target user is not connected to voice.":
                            raise

    @Cog.listener("on_member_update")
    async def process_member_update(self, before: Member, after: Member) -> None:
        if before.roles != after.roles or before.timed_out_until != after.timed_out_until:
            audit_log_updated()

    @Cog.listener("on_message")
    async def moderator_message(self, msg: Message) -> None:
        if msg.channel.type == ChannelType.private:
            if msg.author != client.user and msg.author.id in queued_mods:
                async with sessionmaker() as session:
                    mod = await session.get(TicketMod, msg.author.id,
                        options=(joinedload(TicketMod.queue_top),))
                    if mod is None:
                        return
                    await mod.process_message(msg)
                    async with Ticket.publish_all(session):
                        await session.commit()
                    await session.commit()

    @Cog.listener("on_message")
    async def cleanup_message(self, msg: Message) -> None:
        if conf.cleanup_delay is not None:
            if msg.channel.id == conf.ticket_list:
                if msg.id in cleanup_exempt: return
                await asyncio.sleep(conf.cleanup_delay)
                if msg.id in cleanup_exempt: return
                try:
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

async def find_notes_prefix(session: AsyncSession, prefix: str, *, modid: int, targetid: int) -> List[NoteTicket]:
    stmt = select(NoteTicket).where(NoteTicket.modid == modid, NoteTicket.targetid == targetid,
        NoteTicket.comment.startswith(prefix)).order_by(NoteTicket.id)
    return list((await session.execute(stmt)).scalars())

async def visible_tickets(session: AsyncSession, targetid: int) -> Sequence[Ticket]:
    stmt = select(Ticket).where(Ticket.targetid == targetid, Ticket.status != TicketStatus.HIDDEN)
    return (await session.execute(stmt)).scalars().all()

async def any_visible_tickets(session: AsyncSession, targetid: int) -> bool:
    stmt = select(func.count(Ticket.id)).where(Ticket.targetid == targetid, Ticket.status != TicketStatus.HIDDEN)
    return bool((await session.execute(stmt)).scalar())

async def create_note(session: AsyncSession, note: str, *, modid: int, targetid: int) -> NoteTicket:
    ticket = NoteTicket(
        mod=await TicketMod.get(session, modid),
        targetid=targetid,
        created_at=datetime.utcnow(),
        modified_by=modid,
        stage=TicketStage.COMMENTED,
        status=TicketStatus.IN_EFFECT,
        comment=note)
    session.add(ticket)
    return ticket
