from __future__ import annotations
import re
import itertools
import datetime
import asyncio
import logging
import contextlib
import collections
import enum
import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.ext.asyncio
from typing import (List, Dict, Set, Tuple, Optional, Iterator, AsyncIterator, Sequence, Type, Union, Any, Callable,
    Awaitable, Iterable, Protocol, cast, overload)
import discord
import discord.abc
import discord.ext.commands

import discord_client
import util.db
import util.discord
import util.asyncio
import util.frozen_list

import plugins.reactions
import plugins.privileges
import plugins.cogs
import plugins

logger: logging.Logger = logging.getLogger(__name__)

# ---------- Constants ----------
ticket_comment_re = re.compile(
    r"""
    (?:
    \s*([\d.]+)\s*
    (s(?:ec(?:ond)?s?)?
    |(?-i:m)|min(?:ute)?s?
    |h(?:(?:ou)?rs?)?
    |d(?:ays?)?
    |w(?:(?:ee)?ks?)
    |(?-i:M)|months?
    |y(?:(?:ea)?rs?)?
    )
    |p(?:erm(?:anent)?)?
    )\b\W*
    """, re.VERBOSE | re.IGNORECASE)

time_expansion = {
    's': 1,
    'm': 60,
    'h': 60 * 60,
    'd': 60 * 60 * 24,
    'w': 60 * 60 * 24 * 7,
    'M': 60 * 60 * 24 * 30,
    'y': 60 * 60 * 24 * 365}

# ----------- Config -----------
class TicketsConf(Protocol, Awaitable[None]):
    guild: int # ID of the guild the ticket system is managing
    tracked_roles: util.frozen_list.FrozenList[int] # List of roleids of tracked roles
    last_auditid: Optional[int] # ID of last audit event processed
    ticket_list: int # Channel id of the ticket list in the guild
    prompt_interval: int # How often to ping about delivered tickets
    pending_unmutes: util.frozen_list.FrozenList[int] # List of users peding VC unmute
    pending_undeafens: util.frozen_list.FrozenList[int] # List of users peding VC undeafen
    audit_log_precision: float # How long to allow the audit log to catch up

conf: TicketsConf

@plugins.init
async def init_conf() -> None:
    global conf
    conf = cast(TicketsConf, await util.db.kv.load(__name__))

# ----------- Data -----------

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
@plugins.finalizer
async def cleanup_engine() -> None:
    await engine.dispose()

sessionmaker = sqlalchemy.orm.sessionmaker(engine, class_=sqlalchemy.ext.asyncio.AsyncSession, expire_on_commit=False)

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

ModQueueView = sqlalchemy.Table("mod_queues", sqlalchemy.MetaData(),
    sqlalchemy.Column("id", sqlalchemy.BigInteger), schema="tickets")

@registry.mapped
class TicketMod:
    __tablename__ = "mods"
    __table_args__ = {"schema": "tickets"}
    modid: int = sqlalchemy.Column(sqlalchemy.BigInteger, primary_key=True, autoincrement=False)
    last_read_msgid: int = sqlalchemy.Column(sqlalchemy.BigInteger)
    scheduled_delivery: Optional[datetime.datetime] = sqlalchemy.Column(sqlalchemy.TIMESTAMP)

    queue_top: Optional[Ticket] = sqlalchemy.orm.relationship(lambda: Ticket, primaryjoin=lambda: # type: ignore
            sqlalchemy.and_(TicketMod.modid == Ticket.modid, Ticket.id.in_(sqlalchemy.select(ModQueueView.columns.id))),
        viewonly=True, uselist=False)
        # needs to be refreshed whenever ticket.stage is updated

    @staticmethod
    async def get(session: sqlalchemy.ext.asyncio.AsyncSession, modid: int) -> TicketMod:
        """Get a TicketMod by id, or create if it doesn't exist"""
        mod: Optional[TicketMod] = await session.get(TicketMod, modid)
        if mod is None:
            mod = TicketMod(modid=modid)
            logger.debug("Creating TicketMod {}".format(modid))
            session.add(mod)
        return mod

    async def load_queue(self) -> Optional[Ticket]:
        """Populate the queue_top field"""
        await sqlalchemy.ext.asyncio.async_object_session(self).get(TicketMod, self.modid, # type: ignore
            populate_existing=True, options=(sqlalchemy.orm.joinedload(TicketMod.queue_top),))
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
                    await msg.channel.send(util.discord.format("Ticket #{} was taken by {!m}", ticket.id, modid))
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        ticket.modified_by = actorid
        ticket.delivered_id = None
        new_mod = await TicketMod.get(sqlalchemy.ext.asyncio.async_object_session(self), modid) # type: ignore
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

    async def try_initial_delivery(self, ticket: Ticket) -> None:
        logger.debug(util.discord.format("Delivering Ticket #{} to {!m}", ticket.id, self.modid))
        user = discord_client.client.get_user(self.modid)
        if user is None:
            try:
                user = await discord_client.client.fetch_user(self.modid)
            except discord.NotFound:
                logger.error(util.discord.format("Could not find {!m} to deliver Ticket #{}", self.modid, ticket.id))
                self.scheduled_delivery = datetime.datetime.utcnow() + datetime.timedelta(seconds=conf.prompt_interval)
                return
        try:
            msg = await user.send("Please comment on the following:", embed=ticket.to_embed(dm=True))
        except (discord.NotFound, discord.Forbidden):
            return
        ticket.delivered_id = msg.id
        ticket.stage = TicketStage.DELIVERED
        self.scheduled_delivery = datetime.datetime.utcnow() + datetime.timedelta(seconds=conf.prompt_interval)

    async def try_redelivery(self, ticket: Ticket) -> None:
        logger.debug(util.discord.format("Re-delivering Ticket #{} to {!m}", ticket.id, self.modid))
        user = discord_client.client.get_user(self.modid)
        if user is None:
            try:
                user = await discord_client.client.fetch_user(self.modid)
            except discord.NotFound:
                logger.error(util.discord.format("Could not find {!m} to re-deliver Ticket #{}", self.modid, ticket.id))
                return
        if (msg := await ticket.get_delivery_message(user)) is not None:
            try:
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        try:
            msg = await user.send("Please comment on the following:", embed=ticket.to_embed(dm=True))
        except (discord.NotFound, discord.Forbidden):
            return
        ticket.delivered_id = msg.id
        ticket.stage = TicketStage.DELIVERED
        self.scheduled_delivery = datetime.datetime.utcnow() + datetime.timedelta(seconds=conf.prompt_interval)

    @staticmethod
    def parse_ticket_comment(ticket: Ticket, text: str) -> Tuple[Optional[int], str, str]:
        duration: Optional[int]
        if match := ticket_comment_re.match(text):
            # Extract duration
            if match[1]:
                d = int(match[1])
                token = match[2][0]
                token = token.lower() if token != 'M' else token
                duration = d * time_expansion[token]
            else:
                duration = None
            comment = text[match.end():]
        else:
            duration = None
            comment = text

        msg = ""
        if duration:
            if not ticket.can_revert:
                msg += "Provided duration ignored since this ticket type cannot expire."
                duration = None
            elif ticket.status != TicketStatus.IN_EFFECT:
                msg += "Provided duration ignored since this ticket is no longer in effect."
                duration = None
            else:
                expiry = ticket.created_at + datetime.timedelta(seconds=duration)
                now = datetime.datetime.utcnow()
                if expiry <= now:
                    msg += "Ticket will expire immediately!"
                else:
                    msg += "Ticket will expire in {}.".format(str(expiry - now).split('.')[0])
        return duration, comment, msg

    async def process_message(self, msg: discord.Message) -> None:
        """
        Process a non-command message from the moderator.
        If there is a current active ticket, treat it as a comment.
        Either way, update the last handled message in data.
        """
        prefix = plugins.commands.conf.prefix
        if not prefix or not msg.content.startswith(prefix):
            if (ticket := await self.load_queue()) is not None:
                logger.debug(util.discord.format("Processing message from {!m} as comment to Ticket #{}: {!r}",
                    self.modid, ticket.id, msg.content))

                ticket.duration, ticket.comment, message = self.parse_ticket_comment(ticket, msg.content)

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
    __tablename__ = "tickets"
    __table_args__ = {"schema": "tickets"}
    id: int = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    type: TicketType = sqlalchemy.Column(sqlalchemy.Enum(TicketType, schema="tickets"), nullable=False)
    stage: TicketStage = sqlalchemy.Column(sqlalchemy.Enum(TicketStage, schema="tickets"), nullable=False,
        default=TicketStage.NEW)
    status: TicketStatus = sqlalchemy.Column(sqlalchemy.Enum(TicketStatus, schema="tickets"), nullable=False,
        default=TicketStatus.IN_EFFECT)
    modid: int = sqlalchemy.Column(sqlalchemy.BigInteger, sqlalchemy.ForeignKey(TicketMod.modid), nullable=False)
    targetid: int = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=False)
    auditid: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    duration: Optional[int] = sqlalchemy.Column(sqlalchemy.Integer)
    comment: Optional[str] = sqlalchemy.Column(sqlalchemy.TEXT)
    list_msgid: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    delivered_id: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    created_at: datetime.datetime = sqlalchemy.Column(sqlalchemy.TIMESTAMP, nullable=False,
        default=sqlalchemy.func.current_timestamp().op("AT TIME ZONE")("UTC"))
    modified_by: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)

    mod: TicketMod = sqlalchemy.orm.relationship(TicketMod, lazy="joined")
    __mapper_args__ = {"polymorphic_on": type}

    # Does this ticket type support reverting
    can_revert: bool

    # Action triggering automatic ticket creation
    trigger_action: Optional[discord.AuditLogAction] = None
    # Action triggering automatic ticket reversal
    revert_trigger_action: Optional[discord.AuditLogAction] = None

    @property
    def hidden(self) -> bool:
        return self.status == TicketStatus.HIDDEN

    @property
    def expiry(self) -> Optional[datetime.datetime]:
        if self.can_revert and self.duration is not None:
            return self.created_at + datetime.timedelta(seconds=self.duration)
        return None

    @property
    def jump_link(self) -> str:
        return 'https://discord.com/channels/{}/{}/{}'.format(conf.guild, conf.ticket_list, self.list_msgid)

    @property
    def status_line(self) -> str:
        if self.stage != TicketStage.COMMENTED:
            return self.status.value + ", Uncommented"
        return self.status.value

    def describe(self, *, dm: bool = False) -> str:
        raise NotImplementedError

    def append_comment(self, comment: str) -> None:
        if self.comment is None:
            self.comment = comment
        else:
            self.comment += "\n" + comment

    def to_summary(self, *, dm: bool = False) -> str:
        return util.discord.format("[#{}]({}): {!m} {} ({})", self.id, self.jump_link, self.modid,
            self.describe(dm=dm), self.status_line)

    def to_embed(self, *, dm: bool = False) -> discord.Embed:
        """
        The discord embed describing this ticket.
        """
        embed = discord.Embed(
            title="Ticket #{}".format(self.id),
            description="{} ({})\n{}".format(self.describe(dm=dm), self.status_line, self.comment or ""),
            timestamp=self.created_at)
        embed.add_field(name="Moderator", value=util.discord.format("{!m}", self.modid))

        if self.can_revert:
            if self.expiry is None:
                embed.add_field(name="Permanent", value="\u200E")
            else:
                timestamp = int(self.expiry.replace(tzinfo=datetime.timezone.utc).timestamp())
                embed.add_field(name="Duration", value=str(datetime.timedelta(seconds=self.duration or 0)))
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
            channel = discord_client.client.get_channel(conf.ticket_list)
            if isinstance(channel, discord.TextChannel):
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

        # Run mod ticket update hook
        await self.mod.ticket_updated(self)

    @staticmethod
    @contextlib.asynccontextmanager
    async def publish_all(session: sqlalchemy.ext.asyncio.AsyncSession) -> AsyncIterator[None]:
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

    @staticmethod
    async def create_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        """
        If the audit log entry represents a mod action we care about, create a ticket and return it
        """
        return ()

    @staticmethod
    async def revert_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
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

    async def get_delivery_message(self, user: Optional[discord.User] = None) -> Optional[discord.Message]:
        if self.delivered_id is None:
            return None
        if user is None:
            user = discord_client.client.get_user(self.modid)
        if user is None:
            try:
                user = await discord_client.client.fetch_user(self.modid)
            except discord.NotFound:
                return None
        try:
            return await user.fetch_message(self.delivered_id)
        except discord.NotFound:
            return None

@registry.mapped
class TicketHistory:
    __tablename__ = "history"
    version: int = sqlalchemy.Column(sqlalchemy.Integer)
    last_modified_at: Optional[datetime.datetime] = sqlalchemy.Column(sqlalchemy.TIMESTAMP)
    id: Optional[int] = sqlalchemy.Column(sqlalchemy.Integer,
        sqlalchemy.ForeignKey(Ticket.id, onupdate="CASCADE"))
    type: Optional[TicketType] = sqlalchemy.Column(sqlalchemy.Enum(TicketType, schema="tickets"))
    stage: Optional[TicketStage] = sqlalchemy.Column(sqlalchemy.Enum(TicketStage, schema="tickets"))
    status: Optional[TicketStatus] = sqlalchemy.Column(sqlalchemy.Enum(TicketStatus, schema="tickets"))
    modid: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    targetid: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    roleid: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    auditid: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    duration: Optional[int] = sqlalchemy.Column(sqlalchemy.Integer)
    comment: Optional[str] = sqlalchemy.Column(sqlalchemy.TEXT)
    list_msgid: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    delivered_id: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    created_at: datetime.datetime = sqlalchemy.Column(sqlalchemy.TIMESTAMP)
    modified_by: Optional[int] = sqlalchemy.Column(sqlalchemy.BigInteger)
    __table_args__ = (sqlalchemy.PrimaryKeyConstraint(id, version), {"schema": "tickets"})

# Map of audit actions to the associated handler methods.
create_handlers: Dict[discord.AuditLogAction, List[Callable[[sqlalchemy.ext.asyncio.AsyncSession,
    discord.AuditLogEntry], Awaitable[Sequence[Ticket]]]]] = {}
revert_handlers: Dict[discord.AuditLogAction, List[Callable[[sqlalchemy.ext.asyncio.AsyncSession,
    discord.AuditLogEntry], Awaitable[Sequence[Ticket]]]]] = {}

# Decorator to register Ticket subclasses in action_handlers
def register_action(cls: Type[Ticket]) -> Type[Ticket]:
    if (action := cls.trigger_action) is not None:
        create_handlers.setdefault(action, []).append(cls.create_from_audit)
    if (action := cls.revert_trigger_action) is not None:
        revert_handlers.setdefault(action, []).append(cls.revert_from_audit)
    return cls

@registry.mapped
@register_action
class NoteTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.NOTE} # type: ignore

    can_revert = True

    trigger_action = None
    revert_trigger_action = None

    def describe(self, *, dm: bool = False) -> str:
        return util.discord.format("**Note** for {!m}", self.targetid)

    async def revert_action(self, reason: Optional[str] = None) -> None:
        pass

    async def revert(self, actorid: int) -> None:
        self.status = TicketStatus.HIDDEN
        self.modified_by = actorid

    async def expire(self) -> None:
        self.status = TicketStatus.HIDDEN
        self.modified_by = None

@registry.mapped
@register_action
class KickTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.KICK} # type: ignore

    can_revert = False

    trigger_action = discord.AuditLogAction.kick
    revert_trigger_action = None

    def describe(self, *, dm: bool = False) -> str:
        return util.discord.format("**Kicked** {!m}", self.targetid)

    @staticmethod
    async def create_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        return (KickTicket(
            mod=await TicketMod.get(session, audit.user.id),
            targetid=audit.target.id,
            auditid=audit.id,
            created_at=audit.created_at,
            modified_by=None,
            comment=audit.reason),)

    async def hide(self, actorid: int, reason: Optional[str] = None) -> None:
        logger.debug("Hiding Ticket #{}".format(self.id))
        self.status = TicketStatus.HIDDEN
        self.modified_by = actorid
        if reason is not None:
            self.append_comment(reason)

@registry.mapped
@register_action
class BanTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.BAN} # type: ignore

    can_revert = True

    trigger_action = discord.AuditLogAction.ban
    revert_trigger_action = discord.AuditLogAction.unban

    def describe(self, *, dm: bool = False) -> str:
        return util.discord.format("**Banned** {!m}", self.targetid)

    @staticmethod
    async def create_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        return (BanTicket(
            mod=await TicketMod.get(session, audit.user.id),
            targetid=audit.target.id,
            auditid=audit.id,
            created_at=audit.created_at,
            modified_by=None,
            comment=audit.reason),)

    @staticmethod
    async def revert_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        stmt = sqlalchemy.select(BanTicket).where(
            BanTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
            BanTicket.targetid == audit.target.id)
        return (await session.execute(stmt)).scalars().all()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = discord_client.client.get_guild(conf.guild)
        assert guild
        bans = await guild.bans()
        user = next((entry.user for entry in bans if entry.user.id == self.targetid), None)
        if user is None:
            # User is not banned, nothing to do
            return
        await guild.unban(user, reason=reason)

@registry.mapped
@register_action
class VCMuteTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.VC_MUTE} # type: ignore

    can_revert = True

    trigger_action = discord.AuditLogAction.member_update
    revert_trigger_action = discord.AuditLogAction.member_update

    def describe(self, *, dm: bool = False) -> str:
        return util.discord.format("**VC Muted** {!m}", self.targetid)

    @staticmethod
    async def create_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        if not getattr(audit.before, "mute", True) and getattr(audit.after, "mute", False):
            return (VCMuteTicket(
                mod=await TicketMod.get(session, audit.user.id),
                targetid=audit.target.id,
                auditid=audit.id,
                created_at=audit.created_at,
                modified_by=None,
                comment=audit.reason),)
        else:
            return ()

    @staticmethod
    async def revert_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        if getattr(audit.before, "mute", False) and not getattr(audit.after, "mute", False):
            stmt = sqlalchemy.select(VCMuteTicket).where(
                VCMuteTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                VCMuteTicket.targetid == audit.target.id)
            return (await session.execute(stmt)).scalars().all()
        else:
            return ()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = discord_client.client.get_guild(conf.guild)
        assert guild
        try:
            member = await guild.fetch_member(self.targetid)
        except discord.NotFound:
            # User is no longer in the guild, nothing to do
            return
        try:
            await member.edit(mute=False)
        except discord.HTTPException as exc:
            if exc.text != "Target user is not connected to voice.":
                raise
            conf.pending_unmutes = conf.pending_unmutes + [self.targetid]
            await conf
            logger.debug("Pending unmute for {}".format(self.targetid))

@registry.mapped
@register_action
class VCDeafenTicket(Ticket):
    __mapper_args__ = {"polymorphic_identity": TicketType.VC_DEAFEN} # type: ignore

    can_revert = True

    trigger_action = discord.AuditLogAction.member_update
    revert_trigger_action = discord.AuditLogAction.member_update

    def describe(self, *, dm: bool = False) -> str:
        return util.discord.format("**VC Deafened** {!m}", self.targetid)

    @staticmethod
    async def create_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        if not getattr(audit.before, "deaf", True) and getattr(audit.after, "deaf", False):
            return (VCDeafenTicket(
                mod=await TicketMod.get(session, audit.user.id),
                targetid=audit.target.id,
                auditid=audit.id,
                created_at=audit.created_at,
                modified_by=None,
                comment=audit.reason),)
        else:
            return ()

    @staticmethod
    async def revert_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        if getattr(audit.before, "deaf", False) and not getattr(audit.after, "deaf", False):
            stmt = sqlalchemy.select(VCDeafenTicket).where(
                VCDeafenTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                VCDeafenTicket.targetid == audit.target.id)
            return (await session.execute(stmt)).scalars().all()
        else:
            return ()

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = discord_client.client.get_guild(conf.guild)
        assert guild
        try:
            member = await guild.fetch_member(self.targetid)
        except discord.NotFound:
            # User is no longer in the guild, nothing to do
            return
        try:
            await member.edit(deafen=False)
        except discord.HTTPException as exc:
            if exc.text != "Target user is not connected to voice.":
                raise
            conf.pending_undeafens = conf.pending_undeafens + [self.targetid]
            await conf
            logger.debug("Pending undeafen for {}".format(self.targetid))

@registry.mapped
@register_action
class AddRoleTicket(Ticket):
    roleid: int = sqlalchemy.Column(sqlalchemy.BigInteger)
    __mapper_args__ = {"polymorphic_identity": TicketType.ADD_ROLE, "polymorphic_load": "inline"} # type: ignore

    can_revert = True

    trigger_action = discord.AuditLogAction.member_role_update
    revert_trigger_action = discord.AuditLogAction.member_role_update

    def describe(self, *, dm: bool = False) -> str:
        role_desc = util.discord.format("{!M}", self.roleid)
        if dm:
            if (guild := discord_client.client.get_guild(conf.guild)) and (role := guild.get_role(self.roleid)):
                role_desc = role.name
            else:
                role_desc = str(self.roleid)
        return util.discord.format("**Added Role** {} to {!m}", role_desc, self.targetid)

    @staticmethod
    async def create_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        tickets = []
        for role in audit.changes.after.roles or (): # type: ignore
            if role.id in conf.tracked_roles:
                tickets.append(AddRoleTicket(
                    mod=await TicketMod.get(session, audit.user.id),
                    targetid=audit.target.id,
                    auditid=audit.id,
                    roleid=role.id,
                    created_at=audit.created_at,
                    modified_by=None,
                    comment=audit.reason))
        return tickets

    @staticmethod
    async def revert_from_audit(session: sqlalchemy.ext.asyncio.AsyncSession, audit: discord.AuditLogEntry
        ) -> Sequence[Ticket]:
        tickets: List[Ticket] = []
        for role in audit.changes.before.roles or (): # type: ignore
            if role.id in conf.tracked_roles:
                stmt = sqlalchemy.select(AddRoleTicket).where(
                    AddRoleTicket.status.in_((TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED)),
                    AddRoleTicket.targetid == audit.target.id, AddRoleTicket.roleid == role.id)
                tickets.extend((await session.execute(stmt)).scalars())
        return tickets

    async def revert_action(self, reason: Optional[str] = None) -> None:
        guild = discord_client.client.get_guild(conf.guild)
        assert guild
        role = guild.get_role(self.roleid)
        assert role
        try:
            member = await guild.fetch_member(self.targetid)
        except discord.NotFound:
            # User is no longer in the guild, nothing to do
            return
        await member.remove_roles(role)

@plugins.init
async def init_db() -> None:
    await util.db.init(util.db.get_ddl(
        sqlalchemy.schema.CreateSchema("tickets").execute,
        registry.metadata.create_all,
        sqlalchemy.DDL(r"""
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
        """).execute))

# ----------- Audit logs -----------
audit_log_event = asyncio.Event()

def audit_log_updated() -> None:
    audit_log_event.set()

async def poll_audit_log() -> None:
    """
    Whenever this task is woken up via audit_log_updated, it will read any new audit log events and process them.
    """
    await discord_client.client.wait_until_ready()
    if not conf.guild or not (guild := discord_client.client.get_guild(conf.guild)):
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
            async for entry in guild.audit_logs(limit=None if last else 1, oldest_first=False): # type: ignore
                if last and entry.id <= last:
                    break
                entries.append(entry)
            async with sessionmaker() as session:
                for entry in reversed(entries):
                    try:
                        logger.debug("Processing audit entry {}".format(entry))
                        last = entry.id
                        if entry.user != discord_client.client.user:
                            for create_handler in create_handlers.get(entry.action, ()):
                                for ticket in await create_handler(session, entry):
                                    session.add(ticket)
                                    logger.debug("Created {!r} from audit {}".format(ticket.describe(), entry.id))
                                    await session.commit() # to get ID
                                    await ticket.publish()
                            for revert_handler in revert_handlers.get(entry.action, ()):
                                for ticket in await revert_handler(session, entry):
                                    ticket.status = TicketStatus.REVERTED
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
    await discord_client.client.wait_until_ready()

    while True:
        try:
            async with sessionmaker() as session:
                min_expiry = None
                now = datetime.datetime.utcnow()
                stmt = sqlalchemy.select(Ticket).where(Ticket.status == TicketStatus.IN_EFFECT, Ticket.duration != None)
                for ticket in (await session.execute(stmt)).scalars():
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

            delay = (min_expiry - datetime.datetime.utcnow()).total_seconds() if min_expiry is not None else 86400.0
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
    await discord_client.client.wait_until_ready()

    while True:
        try:
            async with sessionmaker() as session:
                stmt = sqlalchemy.select(TicketMod).options(sqlalchemy.orm.joinedload(TicketMod.queue_top)).where(
                    TicketMod.queue_top != None)
                mods = (await session.execute(stmt)).scalars().all()

                queued_mods = set(mod.modid for mod in mods)
                logger.debug("Listening for comments from {!r}".format(queued_mods))

                min_delivery = None
                now = datetime.datetime.utcnow()
                for mod in mods:
                    if mod.queue_top is None:
                        continue
                    if mod.scheduled_delivery is None or mod.scheduled_delivery <= now:
                        try:
                            if mod.queue_top.stage == TicketStage.NEW:
                                await mod.try_initial_delivery(mod.queue_top)
                            else:
                                await mod.try_redelivery(mod.queue_top)
                        except asyncio.CancelledError:
                            raise
                        except:
                            logger.error(util.discord.format("Exception when delivering a ticket to {!m}", mod.modid),
                                exc_info=True)
                    if min_delivery is None or mod.scheduled_delivery < min_delivery:
                        min_delivery = mod.scheduled_delivery
                # Can't have any publishable changes
                await session.commit()

            delay = (min_delivery - datetime.datetime.utcnow()).total_seconds() if min_delivery is not None else 86400.0
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
    @plugins.finalizer
    def cancel_audit_task() -> None:
        audit_log_task.cancel()
    expiry_task = asyncio.create_task(expire_tickets())
    @plugins.finalizer
    def cancel_expiry_task() -> None:
        expiry_task.cancel()
    delivery_task = asyncio.create_task(deliver_tickets())
    @plugins.finalizer
    def cancel_delivery_task() -> None:
        delivery_task.cancel()
    audit_log_updated()
    expiry_updated()
    delivery_updated()


# ------------ Commands ------------

async def resolve_ticket(ref: Optional[discord.MessageReference],
    ticket_arg: Optional[Union[discord.PartialMessage, int]],
    session: sqlalchemy.ext.asyncio.AsyncSession) -> Ticket:
    """
    Resolves a ticket from the given message and command arg, if possible.
    """
    if isinstance(ticket_arg, int):
        # This is either a message snowflake (a big number) or a ticket id (small number). The leading 42 bits of a
        # snowflake are the timestamp and we assume that if all of those are zero, it's probably not a snowflake as
        # that would imply an epoch time of 0 milliseconds.
        if ticket_arg < 1 << 22:
            ticket = cast(Optional[Ticket], await session.get(Ticket, ticket_arg))
            if ticket is None:
                raise util.discord.InvocationError("No ticket with ID {}".format(ticket_arg))
            return ticket
        else:
            stmt = sqlalchemy.select(Ticket).where(Ticket.list_msgid == ticket_arg)
            ticket = cast(Optional[Ticket], (await session.execute(stmt)).scalars().first())
            if ticket is None:
                raise util.discord.InvocationError("Message ID {} is not referring to a ticket".format(ticket_arg))
            return ticket
    elif isinstance(ticket_arg, discord.PartialMessage):
        stmt = sqlalchemy.select(Ticket).where(Ticket.list_msgid == ticket_arg.id)
        ticket = cast(Optional[Ticket], (await session.execute(stmt)).scalars().first())
        if ticket is None:
            raise util.discord.InvocationError("Message ID {} is not referring to a ticket".format(ticket_arg.id))
        return ticket
    elif ref is not None:
        stmt = sqlalchemy.select(Ticket).where(Ticket.list_msgid == ref.message_id)
        ticket = cast(Optional[Ticket], (await session.execute(stmt)).scalars().first())
        if ticket is None:
            raise util.discord.InvocationError("Message ID {} is not referring to a ticket".format(ref.message_id))
        return ticket
    else:
        raise util.discord.InvocationError("Specify a ticket by ID, message ID, or by replying to it")

def summarise_tickets(tickets: Sequence[Ticket], title: str, *, dm: bool = False
    ) -> Optional[Iterator[discord.Embed]]:
    """
    Create paged embeds of ticket summaries from the provided list of tickets.
    """
    if not tickets:
        return None

    lines = [ticket.to_summary(dm=dm) for ticket in tickets]
    blocks = ['\n'.join(lines[i:i+10]) for i in range(0, len(lines), 10)]
    page_count = len(blocks)

    embeds = (discord.Embed(description=blocks[i], title=title) for i in range(page_count))
    if page_count > 1:
        embeds = (embed.set_footer(text="Page {}/{}".format(i+1, page_count)) for i, embed in enumerate(embeds))
    return embeds

Page = collections.namedtuple('Page', ('content', 'embed'), defaults=(None, None))

async def pager(dest: discord.abc.Messageable, pages: List[Page]) -> None:
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
    msg = await dest.send(**pages[0]._asdict())

    if len(pages) == 1:
        return

    # Add reactions
    for r in reactions:
        await msg.add_reaction(r)

    index = 0
    with plugins.reactions.ReactionMonitor(channel_id=msg.channel.id, message_id=msg.id, event="add",
        filter=lambda _, p: p.user_id != discord_client.client.user.id and p.emoji.name in reactions,
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
                        await dest.send(**page._asdict())
                    break
                index %= len(pages)
                await msg.edit(**pages[index]._asdict())
                try:
                    await msg.remove_reaction(payload.emoji, discord.Object(payload.user_id)) # type: ignore
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

@plugins.cogs.cog
class Tickets(discord.ext.typed_commands.Cog[discord.ext.commands.Context]):
    """Manage infraction history"""
    @discord.ext.commands.command("note")
    @plugins.privileges.priv_ext("mod")
    async def note_command(self, ctx: discord.ext.commands.Context, target: util.discord.PartialUserConverter, *,
        note: Optional[str]) -> None:
        """Create a note on the target user."""
        if note is None:
            # Request the note dynamically
            prompt = await ctx.send("Please enter the note:")
            del_reaction = '\u274C'
            await prompt.add_reaction(del_reaction)
            with plugins.reactions.ReactionMonitor(channel_id=ctx.channel.id, message_id=prompt.id,
                author_id=ctx.author.id, event="add", filter=lambda _, p: p.emoji.name == del_reaction) as mon:
                msg_task = asyncio.create_task(
                    discord_client.client.wait_for('message',
                        check=lambda msg: msg.channel == ctx.channel and msg.author == ctx.author))
                reaction_task = asyncio.ensure_future(mon)
                try:
                    done, pending = await asyncio.wait((msg_task, reaction_task),
                        timeout=300, return_when=asyncio.FIRST_COMPLETED)
                except asyncio.TimeoutError:
                    await ctx.send("Note prompt timed out, please try again.")

                if msg_task in done:
                    note = msg_task.result().content
                elif reaction_task in done:
                    await ctx.send("Note prompt cancelled, no note was created.")
                msg_task.cancel()
                reaction_task.cancel()

        if note is not None:
            async with sessionmaker() as session:
                ticket = NoteTicket(
                    mod=await TicketMod.get(session, ctx.author.id),
                    targetid=target.id,
                    created_at=datetime.datetime.utcnow(),
                    modified_by=ctx.author.id,
                    stage=TicketStage.COMMENTED,
                    status=TicketStatus.IN_EFFECT,
                    comment=note)
                session.add(ticket)
                async with Ticket.publish_all(session):
                    await session.commit()
                await session.commit()

            await ctx.send(embed=discord.Embed(
                description="[#{}]({}): Note created!".format(ticket.id, ticket.jump_link)))

    @discord.ext.commands.group("ticket", aliases=["tickets"])
    @plugins.privileges.priv_ext("mod")
    async def ticket_command(self, ctx: discord.ext.commands.Context) -> None:
        """Manage tickets."""
        pass

    @ticket_command.command("top")
    async def ticket_top(self, ctx: discord.ext.commands.Context) -> None:
        """Re-deliver the ticket at the top of your queue to your DMs."""
        async with sessionmaker() as session:
            mod = await session.get(TicketMod, ctx.author.id,
                options=(sqlalchemy.orm.joinedload(TicketMod.queue_top),))

            if mod is None or mod.queue_top is None:
                await ctx.send("Your queue is empty, good job!")
            else:
                await mod.try_redelivery(mod.queue_top)
                if ctx.channel.type != discord.ChannelType.private:
                    await ctx.send("Ticket #{} has been delivered to your DMs.".format(mod.queue_top.id))

            await session.commit()

    @ticket_command.command("queue")
    async def ticket_queue(self, ctx: discord.ext.commands.Context, mod: Optional[util.discord.PartialUserConverter]
        ) -> None:
        """Show the specified moderator's (or your own) ticket queue."""
        user = ctx.author if mod is None else mod

        async with sessionmaker() as session:
            stmt = sqlalchemy.select(Ticket).where(Ticket.modid == user.id, Ticket.stage != TicketStage.COMMENTED
                ).order_by(Ticket.id)
            tickets = (await session.execute(stmt)).scalars().all()
            embeds = summarise_tickets(tickets, "Queue for {}".format(user),
                dm=ctx.channel.type == discord.ChannelType.private)

        if embeds:
            await pager(ctx.channel, [Page(embed=embed) for embed in embeds])
        else:
            await ctx.send(util.discord.format("{!m} has an empty queue!", user),
                allowed_mentions=discord.AllowedMentions.none())

    @ticket_command.command("take")
    async def ticket_take(self, ctx: discord.ext.commands.Context, ticket: Optional[Union[discord.PartialMessage, int]]
        ) -> None:
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
    async def ticket_assign(self, ctx: discord.ext.commands.Context,
        ticket: Optional[Union[discord.PartialMessage, int]], mod: util.discord.PartialUserConverter) -> None:
        """Assign the specified ticket to the specified moderator."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if mod.id == tkt.modid:
                await ctx.send(util.discord.format("Ticket #{} is already assigned to {!m}", tkt.id, mod.id),
                    allowed_mentions=discord.AllowedMentions.none())
            else:
                await tkt.mod.transfer(tkt, mod.id, actorid=ctx.author.id)
                await tkt.publish()
                await session.commit()

                await ctx.send(util.discord.format("Assigned Ticket #{} to {!m}", tkt.id, mod.id),
                    allowed_mentions=discord.AllowedMentions.none())

    @ticket_command.command("set")
    async def ticket_set(self, ctx: discord.ext.commands.Context,
        ticket: Optional[Union[discord.PartialMessage, int]], *, duration_comment: str) -> None:
        """Set the duration and comment for a ticket."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            tkt.duration, comment, message = TicketMod.parse_ticket_comment(tkt, duration_comment)

            if comment:
                tkt.comment = comment
            tkt.modified_by = ctx.author.id
            await tkt.publish()
            await session.commit()

            await ctx.send(embed=discord.Embed(description="[#{}]({}): Ticket updated. {}".format(
                tkt.id, tkt.jump_link, message)))

    @ticket_command.command("append")
    async def ticket_append(self, ctx: discord.ext.commands.Context,
        ticket: Optional[Union[discord.PartialMessage, int]], *, comment: str) -> None:
        """Append to a ticket's comment."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if len(tkt.comment or "") + len(comment) > 2000:
                raise util.discord.UserError("Cannot append, exceeds maximum comment length!")

            tkt.append_comment(comment)
            tkt.modified_by = ctx.author.id
            await tkt.publish()
            await session.commit()

            await ctx.send(embed=discord.Embed(description="[#{}]({}): Ticket updated.".format(
                tkt.id, tkt.jump_link)))

    @ticket_command.command("revert")
    async def ticket_revert(self, ctx: discord.ext.commands.Context,
        ticket: Optional[Union[discord.PartialMessage, int]]) -> None:
        """Manually revert a ticket."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if not tkt.can_revert:
                raise util.discord.UserError("This ticket type ({}) cannot be reverted!".format(tkt.type.value))
            if not tkt.status in (TicketStatus.IN_EFFECT, TicketStatus.EXPIRE_FAILED):
                await ctx.send(embed=discord.Embed(
                    description=("[#{}]({}): Cannot be reverted as it is no longer active!".format(
                        tkt.id, tkt.jump_link))))
                return

            await tkt.revert(ctx.author.id)
            await tkt.publish()
            await session.commit()

            await ctx.send(embed=discord.Embed(
                description="[#{}]({}): Ticket reverted.".format(tkt.id, tkt.jump_link)))

    @ticket_command.command("hide")
    async def ticket_hide(self, ctx: discord.ext.commands.Context,
        ticket: Optional[Union[discord.PartialMessage, int]], *, comment: Optional[str]) -> None:
        """Hide (and revert) a ticket."""
        async with sessionmaker() as session:
            tkt = await resolve_ticket(ctx.message.reference, ticket, session)
            if tkt.hidden:
                await ctx.send(embed=discord.Embed(description="#{}: Is already hidden!".format(tkt.id)))
                return

            await tkt.hide(ctx.author.id, reason=comment)
            await tkt.publish()
            await session.commit()

            await ctx.send(embed=discord.Embed(description="#{}: Ticket hidden.".format(tkt.id)))

    @ticket_command.command("show")
    async def ticket_show(self, ctx: discord.ext.commands.Context, *,
        user_or_id: Union[util.discord.PartialUserConverter, discord.PartialMessage, int]
        ) -> None:
        """Show tickets affecting given user, or a ticket with a specific ID."""
        async with sessionmaker() as session:
            if isinstance(user_or_id, (discord.PartialMessage, int)):
                tkt = await resolve_ticket(None, user_or_id, session)
                await ctx.send(embed=tkt.to_embed(dm=ctx.channel.type == discord.ChannelType.private))
            else:
                stmt = sqlalchemy.select(Ticket).where(Ticket.targetid == user_or_id.id).order_by(Ticket.id)
                tickets = (await session.execute(stmt)).scalars().all()

                shown = []
                hidden = []
                for tkt in tickets:
                    if tkt.status == TicketStatus.HIDDEN:
                        hidden.append(tkt)
                    else:
                        shown.append(tkt)

                embeds: Optional[Iterable[discord.Embed]] = summarise_tickets(shown,
                    title='Tickets for {}'.format(user_or_id.id),
                    dm=ctx.channel.type == discord.ChannelType.private)
                hidden_field = ', '.join('#{}'.format(tkt.id) for tkt in hidden)

                if hidden_field:
                    embeds = embeds or (discord.Embed(title='Tickets for {}'.format(user_or_id.id)),)
                    embeds = (embed.add_field(name="Hidden", value=hidden_field) for embed in embeds)

                if embeds:
                    await pager(ctx.channel, [Page(embed=embed) for embed in embeds])
                else:
                    await ctx.send("No tickets found for this user.")

    @ticket_command.command("showhidden")
    async def ticket_showhidden(self, ctx: discord.ext.commands.Context, *,
        user_or_id: Union[util.discord.PartialUserConverter, discord.PartialMessage, int]
        ) -> None:
        """Show hidden tickets affecting given user, or a ticket with a specific ID."""
        async with sessionmaker() as session:
            if isinstance(user_or_id, (discord.PartialMessage, int)):
                tkt = await resolve_ticket(None, user_or_id, session)
                await ctx.send(embed=tkt.to_embed(dm=ctx.channel.type == discord.ChannelType.private))
            else:
                stmt = sqlalchemy.select(Ticket).where(
                    Ticket.status == TicketStatus.HIDDEN, Ticket.targetid == user_or_id.id).order_by(Ticket.id)
                tickets = (await session.execute(stmt)).scalars().all()

                embeds = summarise_tickets(tickets, title='Hidden tickets for {}'.format(user_or_id.id),
                    dm=ctx.channel.type == discord.ChannelType.private)

                if embeds:
                    await pager(ctx.channel, [Page(embed=embed) for embed in embeds])
                else:
                    await ctx.send("No hidden tickets found for this user.")

    @discord.ext.commands.Cog.listener("on_member_ban")
    @discord.ext.commands.Cog.listener("on_member_unban")
    @discord.ext.commands.Cog.listener("on_member_remove")
    async def on_member_remove(self, *args: Any) -> None:
        audit_log_updated()


    @discord.ext.commands.Cog.listener("on_voice_state_update")
    async def process_voice_state(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
        ) -> None:
        if before.deaf != after.deaf or before.mute != after.mute:
            audit_log_updated()
        if after.channel is not None:
            if member.id in conf.pending_unmutes:
                try:
                    await member.edit(mute=False)
                    conf.pending_unmutes = util.frozen_list.FrozenList(
                        filter(lambda i: i != member.id, conf.pending_unmutes))
                    logger.debug("Processed unmute for {}".format(member.id))
                except discord.HTTPException as exc:
                    if exc.text != "Target user is not connected to voice.":
                        raise
            if member.id in conf.pending_undeafens:
                try:
                    await member.edit(deafen=False)
                    conf.pending_undeafens = util.frozen_list.FrozenList(
                        filter(lambda i: i != member.id, conf.pending_undeafens))
                    logger.debug("Processed undeafen for {}".format(member.id))
                except discord.HTTPException as exc:
                    if exc.text != "Target user is not connected to voice.":
                        raise

    @discord.ext.commands.Cog.listener("on_member_update")
    async def process_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles != after.roles:
            audit_log_updated()

    @discord.ext.commands.Cog.listener("on_message")
    async def moderator_message(self, msg: discord.Message) -> None:
        if msg.channel.type == discord.ChannelType.private:
            if msg.author.id in queued_mods:
                async with sessionmaker() as session:
                    mod = await session.get(TicketMod, msg.author.id,
                        options=(sqlalchemy.orm.joinedload(TicketMod.queue_top),))
                    if mod is None:
                        return
                    await mod.process_message(msg)
                    async with Ticket.publish_all(session):
                        await session.commit()
                    await session.commit()
