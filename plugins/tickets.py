from __future__ import annotations
import re
import itertools
import datetime
import asyncio
import logging
import contextlib
import collections
import enum
import psycopg2.extensions
from typing import (List, Dict, Tuple, Optional, Iterator, Type, Any, Callable, Awaitable, Iterable, Protocol, cast,
    overload)
import discord
import discord.abc

import discord_client
import util.db
import util.discord
import util.asyncio
import util.frozen_list

import plugins.commands
import plugins.reactions
import plugins.privileges
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
class TicketsConf(Protocol):
    guild: str # ID of the guild the ticket system is managing
    tracked_roles: List[str] # List of roleids of tracked roles
    last_auditid: Optional[str] # ID of last audit event processed
    ticket_list: str # Channel id of the ticket list in the guild
    pending_unmutes: util.frozen_list.FrozenList[int] # List of users peding VC unmute
    pending_undeafens: util.frozen_list.FrozenList[int] # List of users peding VC undeafen

conf = cast(TicketsConf, util.db.kv.Config(__name__))

# ----------- Data -----------
@util.db.init
def init() -> str:
    return r"""
        CREATE SCHEMA tickets;

        CREATE TYPE tickets.TicketType AS ENUM (
            'NOTE',
            'KICK',
            'BAN',
            'VC_MUTE',
            'VC_DEAFEN',
            'ADD_ROLE'
        );

        CREATE TYPE tickets.TicketStatus AS ENUM (
            'NEW',
            'IN_EFFECT',
            'EXPIRED',
            'REVERTED',
            'HIDDEN'
        );

        CREATE TYPE tickets.TicketStage AS ENUM (
            'NEW',
            'DELIVERED',
            'COMMENTED'
        );

        CREATE TABLE tickets.tickets (
            id            SERIAL               PRIMARY KEY,
            type          tickets.TicketType   NOT NULL,
            stage         tickets.TicketStage  NOT NULL,
            status        tickets.TicketStatus NOT NULL,
            modid         BIGINT               NOT NULL,
            targetid      BIGINT               NOT NULL,
            roleid        BIGINT,
            auditid       BIGINT,
            duration      INT,
            comment       TEXT,
            list_msgid    BIGINT,
            delivered_id  BIGINT,
            created_at    TIMESTAMP            NOT NULL,
            modified_by   BIGINT
        );

        CREATE TABLE tickets.mods (
            modid               BIGINT PRIMARY KEY,
            last_read_msgid     BIGINT,
            last_prompt_msgid   BIGINT
        );

        CREATE TABLE tickets.history (
            version             INT,
            last_modified_at    TIMESTAMP,
            id                  INT,
            type                tickets.TicketType,
            stage               tickets.TicketStage,
            status              tickets.TicketStatus,
            modid               BIGINT,
            targetid            BIGINT,
            roleid              BIGINT,
            auditid             BIGINT,
            duration            INT,
            comment             TEXT,
            list_msgid          BIGINT,
            delivered_id        BIGINT,
            created_at          TIMESTAMP,
            modified_by         BIGINT,
            PRIMARY KEY (id, version),
            FOREIGN KEY (id) REFERENCES tickets.tickets ON UPDATE CASCADE
        );

        CREATE FUNCTION tickets.log_ticket_update()
        RETURNS TRIGGER AS $log_ticket_update$
            DECLARE
                modified tickets.tickets%rowtype;
                last_version int;
            BEGIN
                SELECT INTO modified
                    NEW.id,
                    NULLIF(NEW.type, OLD.type),
                    NULLIF(NEW.stage, OLD.stage),
                    NULLIF(NEW.status, OLD.status),
                    NULLIF(NEW.modid, OLD.modid),
                    NULLIF(NEW.targetid, OLD.targetid),
                    NULLIF(NEW.roleid, OLD.roleid),
                    NULLIF(NEW.auditid, OLD.auditid),
                    NULLIF(NEW.duration, OLD.duration),
                    NULLIF(NEW.comment, OLD.comment),
                    NULLIF(NEW.list_msgid, OLD.list_msgid),
                    NULLIF(NEW.delivered_id, OLD.delivered_id),
                    NULLIF(NEW.created_at, OLD.created_at),
                    NEW.modified_by;

                SELECT   version INTO last_version
                FROM     tickets.history
                WHERE    id = OLD.id
                ORDER BY version DESC LIMIT 1;

                IF NOT FOUND THEN
                    INSERT INTO
                        tickets.history
                    VALUES
                        (0, OLD.created_at, OLD.*),
                        (1, now(), modified.*);
                ELSE
                    INSERT INTO
                        tickets.history
                    VALUES
                        (coalesce(last_version + 1, 1), now(), modified.*);
                END IF;
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
        """


class FieldConstants(enum.Enum):
    """
    A collection of database field constants to use for selection conditions.
    """
    NULL = "IS NULL"
    NOTNULL = "IS NOT NULL"


class RowInterface:
    __slots__ = ('_row', '_pending')
    _row: Tuple[Any, ...]
    _pending: Optional[Dict[str, Any]]

    _conn = util.db.connection()

    _table: str
    _id_col: int
    _columns: Tuple[str, ...] = ()

    def __init__(self, row: Tuple[Any, ...], *args: Any, **kwargs: Any):
        self._row = row
        self._pending = None

    def __repr__(self) -> str:
        return "{}({})".format( self.__class__.__name__,
            ', '.join("{}={!r}".format(col, self._row[i]) for i, col in enumerate(self._columns)))

    def __getattr__(self, key: str) -> Any:
        if key in self._columns:
            if self._pending and key in self._pending:
                return self._pending[key]
            else:
                return self._row[self._columns.index(key)]
        else:
            raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._columns:
            if self._pending is None:
                self.update(**{key: value})
            else:
                self._pending[key] = value
        else:
            super().__setattr__(key, value)

    @contextlib.contextmanager
    def batch_update(self) -> Iterator[Dict[str, Any]] :
        if self._pending is not None:
            raise ValueError("Nested batch updates for {}!".format(self.__class__.__name__))

        self._pending = {}
        try:
            yield self._pending
        finally:
            self.update(**self._pending)
            self._pending = None

    def _refresh(self) -> None:
        rows = self._select_where(**{self._columns[self._id_col]: self._row[self._id_col]})
        if not rows:
            raise ValueError("Refreshing a {} which no longer exists!".format(self.__class__.__name__))
        self._row = rows[0]

    def update(self, **values: Any) -> None:
        rows = self._update_where(values, **{self._columns[self._id_col]: self._row[self._id_col]})
        if not rows:
            raise ValueError("Updating a {} which no longer exists!".format(self.__class__.__name__))
        self._row = rows[0]

    @staticmethod
    def _format_conditions(conditions: Dict[str, Any]) -> Tuple[str, Tuple[Any, ...]]:
        if not conditions:
            return ("", tuple())

        values = []
        conditional_strings = []
        for key, item in conditions.items():
            if isinstance(item, (list, tuple)):
                conditional_strings.append("{} IN %s".format(key))
                values.append(tuple(item))
            elif isinstance(item, FieldConstants):
                conditional_strings.append("{} {}".format(key, item.value))
            else:
                conditional_strings.append("{}=%s".format(key))
                values.append(item)

        return (' AND '.join(conditional_strings), tuple(values))

    @classmethod
    def _select_where(cls, _extra: Optional[str] = None, **conditions: Any) -> List[Tuple[Any, ...]]:
        with cls._conn as conn:
            with conn.cursor() as cursor:
                cond_str, cond_values = cls._format_conditions(conditions)

                cursor.execute(
                    "SELECT * FROM {} {} {} {}".format(
                        cls._table,
                        'WHERE' if conditions else '',
                        cond_str,
                        _extra or ''
                    ), cond_values)
                return cursor.fetchall()

    @classmethod
    def _insert(cls, **values: Any) -> Tuple[Any, ...]:
        with cls._conn as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO {} ({}) VALUES %s RETURNING *".format(
                        cls._table,
                        ", ".join(values.keys()),
                    ), (tuple(values.values()),))
                return cursor.fetchone() or ()

    @classmethod
    def _update_where(cls, values: Dict[str, Any], **conditions: Any) -> List[Tuple[Any, ...]]:
        with cls._conn as conn:
            with conn.cursor() as cursor:
                cond_str, cond_values = cls._format_conditions(conditions)
                cursor.execute(
                    "UPDATE {} SET ({}) = ROW %s WHERE {} RETURNING *".format(
                        cls._table,
                        ", ".join(values.keys()),
                        cond_str
                    ), (tuple(values.values()), *cond_values))
                return cursor.fetchall()


# ----------- Tickets -----------

class FieldEnum(str, enum.Enum):
    """
    String enum with description conforming to the ISQLQuote protocol.
    Allows processing by psycog
    """
    value: str
    desc: str

    @overload # type: ignore
    def __new__(cls, value: str) -> FieldEnum: ...
    def __new__(cls, value: str, desc: str) -> FieldEnum: # type: ignore
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.desc = desc
        return obj

    def __repr__(self) -> str:
        return '<%s.%s>' % (self.__class__.__name__, self.name)

    def __bool__(self) -> bool:
        return True

    def __conform__(self, proto: Any) -> psycopg2.extensions.QuotedString:
        return psycopg2.extensions.QuotedString(self.value)

class TicketType(FieldEnum):
    """
    The possible ticket types.
    Types are represented as the corresponding moderation action.
    """
    NOTE = 'NOTE', 'Note'
    KICK = 'KICK', 'Kicked'
    BAN = 'BAN', 'Banned'
    VC_MUTE = 'VC_MUTE', 'Muted'
    VC_DEAFEN = 'VC_DEAFEN', 'Deafened'
    ADD_ROLE = 'ADD_ROLE', 'Role added'


class TicketStatus(FieldEnum):
    """
    Possible values for the current status of a ticket.
    """
    # New, uncommented and active ticket
    NEW = 'NEW', 'New'
    # Commented and active ticket
    IN_EFFECT = 'IN_EFFECT', 'In effect'
    # Ticket's duration has expired, may be (un)commented
    EXPIRED = 'EXPIRED', 'Expired'
    # Ticket has been manually reverted, may be (un)commented
    REVERTED = 'REVERTED', 'Manually reverted'
    # Ticket is inactive and has been hidden, may be (un)commented
    HIDDEN = 'HIDDEN', 'Hidden'


class TicketStage(FieldEnum):
    """
    The possible stages of delivery of a ticket to the responsible moderator.
    """
    NEW = 'NEW', 'New'
    DELIVERED = 'DELIVERED', 'Delivered'
    COMMENTED = 'COMMENTED', 'Commented'


class Ticket(RowInterface):
    __slots__ = ()

    _table = 'tickets.tickets'
    _id_col = 0
    _columns = (
        'id',
        'type',
        'stage',
        'status',
        'modid',
        'targetid',
        'roleid',
        'auditid',
        'duration',
        'comment',
        'list_msgid',
        'delivered_id',
        'created_at',
        'modified_by',
    )
    id: int
    type: TicketType
    stage: TicketStage
    status: TicketStatus
    modid: int
    targetid: int
    roleid: Optional[int]
    auditid: Optional[int]
    duration: Optional[int]
    comment: Optional[str]
    list_msgid: Optional[int]
    delivered_id: Optional[int]
    created_at: datetime.datetime
    modified_by: Optional[int]

    _type: TicketType
    title: str # Friendly human readable title used for ticket embeds
    can_revert: bool # Whether this ticket type can expire

    # Action triggering automatic ticket creation
    trigger_action: Optional[discord.AuditLogAction] = None
    # Action triggering automatic ticket reversal
    revert_trigger_action: Optional[discord.AuditLogAction] = None

    @property
    def embed(self) -> discord.Embed:
        """
        The discord embed describing this ticket.
        """
        user = discord_client.client.get_user(self.modid)
        embed = discord.Embed(
            title=self.title,
            description=self.comment or "No comment",
            timestamp=self.created_at
        ).set_author(
            name="Ticket #{} ({})".format(self.id, TicketStatus(self.status).desc)
        ).set_footer(
            text="Moderator: {}".format(user or self.modid)
        ).add_field(
            name="Target", value=util.discord.format("{0!m}\n({0})", self.targetid)
        )

        if self.roleid:
            if (role := self.role):
                value = "{}\n({})".format(role.name, role.id)
            else:
                value = str(self.roleid)
            embed.add_field(name="Role", value=value)

        if self.duration:
            embed.add_field(name="Duration", value=str(datetime.timedelta(seconds=self.duration)))
        else:
            embed.add_field(name="Duration", value="Permanent")
        return embed

    @property
    def history(self) -> None:
        """
        The modification history of this ticket.
        """
        pass

    @property
    def hidden(self) -> bool:
        """
        Whether this ticket is hidden
        """
        return self.status == TicketStatus.HIDDEN

    @property
    def active(self) -> bool:
        """
        Whether this ticket is active, i.e. either new or in effect
        """
        return self.status in [TicketStatus.NEW, TicketStatus.IN_EFFECT]

    @property
    def expiry(self) -> Optional[datetime.datetime]:
        """
        Expiry timestamp for this ticket, if applicable.
        """
        if self.can_revert and self.duration is not None:
            return self.created_at + datetime.timedelta(seconds=self.duration)
        return None

    @property
    def mod(self) -> TicketMod:
        """
        TicketMod associated to this ticket.
        """
        return get_or_create_mod(self.modid)

    @property
    def target(self) -> Optional[discord.Member]:
        guild = discord_client.client.get_guild(int(conf.guild))
        if guild is None: return None
        return guild.get_member(self.targetid)

    @property
    def role(self) -> Optional[discord.Role]:
        if self.roleid is None: return None
        guild = discord_client.client.get_guild(int(conf.guild))
        if guild is None: return None
        return guild.get_role(self.roleid)

    @property
    def jump_link(self) -> str:
        return 'https://discord.com/channels/{}/{}/{}'.format(conf.guild, conf.ticket_list, self.list_msgid)

    def summary(self, fmt: Optional[str] = None) -> str:
        """
        A short one-line summary of the ticket.
        """
        fmt = fmt or ("[#{id}]({jump_link})(`{status:<9}`): **{type}** for {targetid!m} by {modid!m}.")

        fmt_dict = {col: self._row[i] for i, col in enumerate(self._columns)}
        fmt_dict['status'] = TicketStatus(self.status).name
        fmt_dict['stage'] = TicketStage(self.stage).name
        fmt_dict['type'] = TicketType(self.type).name

        return util.discord.format(
            fmt,
            ticket=self,
            title=self.title,
            jump_link=self.jump_link,
            **fmt_dict)

    async def publish(self) -> None:
        """
        Ticket update hook.
        Should be run whenever a ticket is created or updated.
        Manages the ticket list embed.
        Defers to the expiry and ticket mod update hooks.
        """
        # Reschedule or cancel ticket expiry if required
        expiration_updated.release()

        # Post to or update the ticket list
        if conf.ticket_list:
            channel = discord_client.client.get_channel(int(conf.ticket_list))
            if isinstance(channel, discord.TextChannel):
                message = None
                if self.list_msgid:
                    try:
                        message = await channel.fetch_message(self.list_msgid)
                    except discord.NotFound:
                        pass

                if message is not None:
                    if not self.hidden:
                        try:
                            await message.edit(embed=self.embed)
                        except discord.HTTPException:
                            message = None
                    else:
                        try:
                            await message.delete()
                            self.list_msgid = None
                        except discord.HTTPException:
                            pass

                if message is None and not self.hidden:
                    message = await channel.send(embed=self.embed)
                    self.list_msgid = message.id

        # Run mod ticket update hook
        await self.mod.ticket_updated(self)

    @classmethod
    def create(cls, **kwargs: Any) -> Ticket:
        """
        Creates a new ticket from the given `kwargs`.
        The `kwargs` must be a collection of column/value pairs to insert.
        """
        row = cls._insert(**kwargs)
        ticket = cls(row)
        logger.debug("Ticket created: {!r}".format(ticket))
        return ticket

    @classmethod
    async def create_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a *creation* audit entry.
        Create a new ticket from the entry data if required.
        """
        raise NotImplementedError

    @classmethod
    async def revert_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a *revert* audit entry.
        Revert a ticket from the entry data if required.
        """
        raise NotImplementedError

    async def revert_action(self, reason: Optional[str] = None) -> bool:
        """
        Attempt to reverse the ticket moderation action.
        Transparently re-raise exceptions.
        """
        raise NotImplementedError

    async def expire(self, **kwargs: Any) -> None:
        """
        Automatically expire the ticket.
        """
        # TODO: Expiry error handling
        result = await self.revert_action(reason="Ticket #{}: Automatic expiry.".format(self.id))
        if result:
            self.update(status=TicketStatus.EXPIRED, modified_by=0)
            await self.publish()

    async def manual_revert(self, actorid: int, **kwargs: Any) -> bool:
        """
        Manually revert the ticket.
        """
        result = await self.revert_action(
            reason="Ticket #{}: Moderator {} requested revert.".format(self.id, actorid))
        if result:
            self.update(status=TicketStatus.REVERTED, modified_by=actorid)
            await self.publish()
        return result

    async def hide(self, actorid: int, reason: Optional[str] = None, **kwargs: Any) -> bool:
        """
        Revert a ticket and set its status to HIDDEN.
        """
        result = await self.revert_action(
            reason="Ticket #{}: Moderator {} hid the ticket.".format(self.id, actorid))
        if result:
            with self.batch_update():
                self.status = TicketStatus.HIDDEN
                self.modified_by = actorid
                if reason is not None:
                    if self.comment is None:
                        self.comment = reason
                    else:
                        self.comment = self.comment + '\n' + reason
            await self.publish()
        return result

# Map of ticket types to the associated class.
ticket_types: Dict[TicketType, Type[Ticket]] = {}
# Map of audit actions to the associated handler methods.
action_handlers: Dict[discord.AuditLogAction, List[Callable[[discord.AuditLogEntry], Awaitable[None]]]] = {}

# Decorator to register Ticket subclasses for each TicketType
def ticket_type(cls: Type[Ticket]) -> Type[Ticket]:
    ticket_types[cls._type] = cls
    if (action := cls.trigger_action) is not None:
        handlers = action_handlers.setdefault(action, [])
        handlers.append(cls.create_from_audit)

    if (action := cls.revert_trigger_action) is not None:
        handlers = action_handlers.setdefault(action, [])
        handlers.append(cls.revert_from_audit)
    return cls


@ticket_type
class NoteTicket(Ticket):
    _type = TicketType.NOTE

    title = "Note"
    can_revert = True

    trigger_action = None
    revert_trigger_action = None

    async def revert_action(self, reason: Optional[str] = None) -> bool:
        """
        Notes have no revert action
        """
        return True

    async def manual_revert(self, actorid: int, **kwargs: Any) -> bool:
        """
        Manually reverted notes are hidden.
        """
        self.update(status=TicketStatus.HIDDEN, modified_by=actorid)
        await self.publish()
        return True

    async def expire(self, **kwargs: Any) -> None:
        """
        Expiring notes are hidden.
        """
        self.update(status=TicketStatus.HIDDEN, modified_by=0)
        await self.publish()


@ticket_type
class KickTicket(Ticket):
    _type = TicketType.KICK

    title = "Kick"
    can_revert = False

    trigger_action = discord.AuditLogAction.kick
    revert_trigger_action = None

    @classmethod
    async def create_from_audit(cls, audit: discord.AuditLogEntry
        ) -> None:
        """
        Handle a kick audit event.
        """
        await cls.create(
            type=cls._type,
            stage=TicketStage.NEW,
            status=TicketStatus.NEW,
            modid=audit.user.id,
            targetid=audit.target.id,
            auditid=audit.id,
            roleid=None,
            created_at=audit.created_at,
            modified_by=0,
            comment=audit.reason
        ).publish()


@ticket_type
class BanTicket(Ticket):
    _type = TicketType.BAN

    title = "Ban"
    can_revert = True

    trigger_action = discord.AuditLogAction.ban
    revert_trigger_action = discord.AuditLogAction.unban

    @classmethod
    async def create_from_audit(cls, audit: discord.AuditLogEntry
        ) -> None:
        """
        Handle a ban audit event.
        """
        await cls.create(
            type=cls._type,
            stage=TicketStage.NEW,
            status=TicketStatus.NEW,
            modid=audit.user.id,
            targetid=audit.target.id,
            auditid=audit.id,
            roleid=None,
            created_at=audit.created_at,
            modified_by=0,
            comment=audit.reason
        ).publish()

    @classmethod
    async def revert_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle an unban audit event.
        """
        # Select any relevant tickets
        tickets = fetch_tickets_where(
            type=cls._type,
            targetid=audit.target.id,
            status=[TicketStatus.NEW, TicketStatus.IN_EFFECT])
        for ticket in tickets:
            ticket.update(status=TicketStatus.REVERTED, modified_by=audit.user.id)
            await ticket.publish()

    async def revert_action(self, reason: Optional[str] = None) -> bool:
        """
        Unban the acted user, if possible.
        """
        guild = discord_client.client.get_guild(int(conf.guild))
        if guild is None: return False
        bans = await guild.bans()
        user = next((entry.user for entry in bans if entry.user.id == self.targetid), None)
        if user is None:
            # User is not banned, nothing to do
            return True
        await guild.unban(user, reason=reason)
        return True


@ticket_type
class VCMuteTicket(Ticket):
    _type = TicketType.VC_MUTE

    title = "VC Mute"
    can_revert = True

    trigger_action = discord.AuditLogAction.member_update
    revert_trigger_action = discord.AuditLogAction.member_update

    @classmethod
    async def create_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a VC mute event.
        """
        if not hasattr(audit.before, "mute"):
            return
        if not audit.before.mute and audit.after.mute: # type: ignore
            await cls.create(
                type=cls._type,
                stage=TicketStage.NEW,
                status=TicketStatus.NEW,
                modid=audit.user.id,
                targetid=audit.target.id,
                auditid=audit.id,
                roleid=None,
                created_at=audit.created_at,
                modified_by=0,
                comment=audit.reason
            ).publish()

    @classmethod
    async def revert_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a VC unmute event
        """
        if not hasattr(audit.before, "mute"):
            return
        if audit.before.mute and not audit.after.mute: # type: ignore
            # Select any relevant tickets
            tickets = fetch_tickets_where(
                type=cls._type,
                targetid=audit.target.id,
                status=[TicketStatus.NEW, TicketStatus.IN_EFFECT])
            for ticket in tickets:
                ticket.update(status=TicketStatus.REVERTED, modified_by=audit.user.id)
                await ticket.publish()

    async def revert_action(self, reason: Optional[str] = None) -> bool:
        """
        Attempt to unmute the target user.
        """
        guild = discord_client.client.get_guild(int(conf.guild))
        if guild is None: return False
        member = guild.get_member(self.targetid)
        if member is None:
            # User is no longer in the guild, nothing to do
            return True
        try:
            await member.edit(mute=False)
        except discord.HTTPException as exc:
            if exc.text != "Target user is not connected to voice.":
                raise
            conf.pending_unmutes = conf.pending_unmutes + [self.targetid]
            logging.debug("Pending unmute for {}".format(self.targetid))
        return True


@ticket_type
class VCDeafenTicket(Ticket):
    _type = TicketType.VC_DEAFEN

    title = "VC Deafen"
    can_revert = True

    trigger_action = discord.AuditLogAction.member_update
    revert_trigger_action = discord.AuditLogAction.member_update

    @classmethod
    async def create_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a VC deafen event.
        """
        if not hasattr(audit.before, "deaf"):
            return
        if not audit.before.deaf and audit.after.deaf: # type: ignore
            await cls.create(
                type=cls._type,
                stage=TicketStage.NEW,
                status=TicketStatus.NEW,
                modid=audit.user.id,
                targetid=audit.target.id,
                auditid=audit.id,
                roleid=None,
                created_at=audit.created_at,
                modified_by=0,
                comment=audit.reason
            ).publish()

    @classmethod
    async def revert_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a VC undeafen event
        """
        if not hasattr(audit.before, "deaf"):
            return
        if audit.before.deaf and not audit.after.deaf: # type: ignore
            # Select any relevant tickets
            tickets = fetch_tickets_where(
                type=cls._type,
                targetid=audit.target.id,
                status=[TicketStatus.NEW, TicketStatus.IN_EFFECT])
            for ticket in tickets:
                ticket.update(status=TicketStatus.REVERTED, modified_by=audit.user.id)
                await ticket.publish()

    async def revert_action(self, reason: Optional[str] = None) -> bool:
        """
        Attempt to undeafen the target user.
        """
        guild = discord_client.client.get_guild(int(conf.guild))
        if guild is None: return False
        member = guild.get_member(self.targetid)
        if member is None:
            # User is no longer in the guild, nothing to do
            return True
        try:
            await member.edit(deafen=False)
        except discord.HTTPException as exc:
            if exc.text != "Target user is not connected to voice.":
                raise
            conf.pending_undeafens = conf.pending_undeafens + [self.targetid]
            logging.debug("Pending undeafen for {}".format(self.targetid))
        return True


@ticket_type
class AddRoleTicket(Ticket):
    _type = TicketType.ADD_ROLE

    roleid: int

    title = "Role Added"
    can_revert = True

    trigger_action = discord.AuditLogAction.member_role_update
    revert_trigger_action = discord.AuditLogAction.member_role_update

    @classmethod
    async def create_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a tracked role add event.
        """
        if audit.changes.after.roles: # type: ignore
            for role in audit.changes.after.roles: # type: ignore
                if conf.tracked_roles and str(role.id) in conf.tracked_roles:
                    await cls.create(
                        type=cls._type,
                        stage=TicketStage.NEW,
                        status=TicketStatus.NEW,
                        modid=audit.user.id,
                        targetid=audit.target.id,
                        auditid=audit.id,
                        roleid=role.id,
                        created_at=audit.created_at,
                        modified_by=0,
                        comment=audit.reason
                    ).publish()

    @classmethod
    async def revert_from_audit(cls, audit: discord.AuditLogEntry) -> None:
        """
        Handle a tracked role remove event.
        """
        if audit.changes.before.roles: # type: ignore
            for role in audit.changes.before.roles: # type: ignore
                if conf.tracked_roles and str(role.id) in conf.tracked_roles:
                    # Select any relevant tickets
                    tickets = fetch_tickets_where(
                        type=cls._type,
                        targetid=audit.target.id,
                        roleid=role.id,
                        status=[TicketStatus.NEW, TicketStatus.IN_EFFECT]
                    )
                    for ticket in tickets:
                        ticket.update(
                            status=TicketStatus.REVERTED,
                            modified_by=audit.user.id
                        )
                        await ticket.publish()

    async def revert_action(self, reason: Optional[str] = None) -> bool:
        """
        Attempt to remove the associated role from the target.
        """
        guild = discord_client.client.get_guild(int(conf.guild))
        if guild is None: return False
        role = guild.get_role(self.roleid)
        if role is None: return False
        target = guild.get_member(self.targetid)
        if target is None: return True
        await target.remove_roles(role)
        return True

audit_log_updated = asyncio.Semaphore(value=0)

async def update_audit_log(*args: Any) -> None:
    audit_log_updated.release()

async def read_audit_log() -> None:
    """
    Whenever this task is woken up via _audit_log_updated, it will read any new audit log events and process them.
    """
    await discord_client.client.wait_until_ready()
    if not conf.guild or not (guild := discord_client.client.get_guild(int(conf.guild))):
        logger.error("Guild not configured, or can't find the configured guild! Cannot read audit log.")
        return

    last = conf.last_auditid and int(conf.last_auditid)
    while True:
        try:
            try:
                await asyncio.wait_for(audit_log_updated.acquire(), timeout=600)
                await asyncio.sleep(1)
                while True:
                    await asyncio.wait_for(audit_log_updated.acquire(), timeout=0)
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
            for entry in reversed(entries):
                try:
                    logger.debug("Processing audit entry {}".format(entry))
                    last = entry.id
                    if entry.user != discord_client.client.user:
                        if entry.action in action_handlers:
                            for handler in action_handlers[entry.action]:
                                await handler(entry)
                except asyncio.CancelledError:
                    raise
                except:
                    logger.error("Processing audit entry {}".format(entry),
                        exc_info=True)

        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in audit log task", exc_info=True)
            await asyncio.sleep(60)
        finally:
            conf.last_auditid = str(last) if last is not None else None

audit_log_task: asyncio.Task[None] = util.asyncio.run_async(read_audit_log)
@plugins.finalizer
def cancel_audit_task() -> None:
    audit_log_task.cancel()

def fetch_tickets_where(**kwargs: Any) -> Iterator[Ticket]:
    """
    Fetch Tickets matching the given conditions.
    Values must be given in data-compatible form.
    Lists of values are supported and will be converted to `IN` conditionals.
    """
    rows = Ticket._select_where(**kwargs)
    return (ticket_types[TicketType(row[Ticket._columns.index('type')])](row) for row in rows)


async def create_ticket(type: TicketType, modid: int, targetid: int, created_at: datetime.datetime, created_by: int,
    stage: Optional[TicketStage] = None, status: Optional[TicketStatus] = None, auditid: Optional[int] = None,
    roleid: Optional[int] = None, comment: Optional[str] = None, duration: Optional[int] = None) -> Ticket:
    # Get the appropriate Ticket subclass
    TicketClass = ticket_types[type]

    # Create and publish the ticket
    ticket = TicketClass.create(
        type=type,
        stage=(stage or TicketStage.NEW),
        status=(status or TicketStatus.NEW),
        modid=modid,
        targetid=targetid,
        auditid=auditid,
        roleid=roleid,
        created_at=created_at,
        modified_by=created_by,
        duration=duration,
        comment=comment)
    await ticket.publish()

    return ticket


def get_ticket(ticketid: int) -> Optional[Ticket]:
    tickets = fetch_tickets_where(id=ticketid)
    return next(tickets, None)


# ----------- Ticket expiry system -----------
expiration_updated = asyncio.Semaphore(value=0)

async def expire_tickets() -> None:
    await discord_client.client.wait_until_ready()

    while True:
        try:
            expiring_tickets = fetch_tickets_where(
                status=[TicketStatus.NEW, TicketStatus.IN_EFFECT],
                duration=FieldConstants.NOTNULL,
            )
            now = datetime.datetime.utcnow().timestamp()
            next_expiring = None
            for ticket in expiring_tickets:
                assert ticket.expiry is not None
                if ticket.expiry.timestamp() < now:
                    try:
                        logger.debug("Expiring Ticket #{}".format(ticket.id))
                        await ticket.expire()
                    except asyncio.CancelledError:
                        raise
                    except:
                        logger.error("Exception when expiring Ticket #{}".format(ticket.id), exc_info=True)
                elif (next_expiring is None or ticket.expiry.timestamp() < next_expiring.expiry.timestamp()):
                    next_expiring = ticket

            delay = 86400.0
            if next_expiring is not None:
                assert next_expiring.expiry is not None
                delay = next_expiring.expiry.timestamp() - now
                logger.debug("Waiting for Ticket #{} to expire (in {} seconds)".format(next_expiring.id, delay))
            try:
                await asyncio.wait_for(expiration_updated.acquire(), timeout=delay)
                while True:
                    await asyncio.wait_for(expiration_updated.acquire(), timeout=1)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in ticket expiry task", exc_info=True)
            await asyncio.sleep(60)

expiry_task: asyncio.Task[None] = util.asyncio.run_async(expire_tickets)
@plugins.finalizer
def cancel_expiry() -> None:
    expiry_task.cancel()

# ----------- Ticket Mods and queue management -----------
ticketmods: Dict[int, TicketMod] = {}

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
        elif not ticket.active:
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

class TicketMod(RowInterface):
    __slots__ = (
        'current_ticket',
        'prompt_task',
        'current_msg'
    )
    current_ticket: Optional[Ticket]
    prompt_task: Optional[asyncio.Task[None]]
    current_msg: Optional[discord.Message]

    _table = 'tickets.mods'
    _id_col = 0
    _columns = (
        'modid',
        'last_read_msgid',
        'last_prompt_msgid',
    )
    modid: int
    last_read_msgid: Optional[int]
    last_prompt_msgid: Optional[int]

    prompt_interval = 12 * 60 * 60

    def __init__(self, row: Tuple[Any, ...]):
        super().__init__(row)
        self.current_ticket = self.get_current_ticket()
        self.prompt_task = None
        self.current_msg = None
        logger.debug("Initialised ticket mod {}. Next ticket: {}".format(self.modid, self.current_ticket))

    @property
    def queue(self) -> Iterator[Ticket]:
        return fetch_tickets_where(
            modid=self.modid,
            stage=[TicketStage.DELIVERED, TicketStage.NEW],
            _extra="ORDER BY stage DESC, id ASC")

    async def find_user(self) -> discord.User:
        """
        Return the discord User object associated to this moderator.
        """
        if not (user := discord_client.client.get_user(self.modid)):
            user = await discord_client.client.fetch_user(self.modid)
        return user

    async def get_ticket_message(self) -> Optional[discord.Message]:
        """
        Get the current ticket delivery message in the DM, if it exists.
        """
        ticket = self.current_ticket
        if ticket and (msgid := ticket.delivered_id):
            if not self.current_msg or self.current_msg.id != msgid:
                # Update the cached message
                user = await self.find_user()
                self.current_msg = await user.fetch_message(msgid)
            return self.current_msg
        return None

    async def load(self) -> None:
        """
        Initial TicketMod loading to be run on initial launch.
        Safe to run outside of launch.
        Processes any missed messages from the moderator.
        Also schedules prompt and/or delivery if required.
        """
        if (ticket := self.current_ticket):
            logger.debug("Loading moderator {}.".format(self.modid))
            if ticket.stage == TicketStage.NEW:
                # The ticket at the top of their queue wasn't delivered
                # The last ticket was delivered, but not yet commented
                # Replay any messages we missed
                # Process the first message as a comment, if it exists

                # Message snowflake to process from
                last_read = discord.Object(max(self.last_read_msgid or 0, ticket.delivered_id or 0))

                # Collect the missed messages
                mod_messages = []
                try:
                    user = await self.find_user()
                    messages = await user.history(after=last_read, limit=None).flatten()
                    mod_messages = [msg for msg in messages if msg.author.id == self.modid]
                except discord.NotFound:
                    pass

                if mod_messages:
                    logger.debug("Missed {} messages from moderator {}.".format(len(mod_messages), self.modid))

                    # Process the first missed message
                    await self.process_message(mod_messages[0])
                    # Save the last missed message as the last one handled
                    if len(mod_messages) > 1:
                        self.last_read_msgid = mod_messages[-1].id
                else:
                    # Schedule the reminder prompt for the current ticket
                    await self.schedule_prompt()

    def unload(self) -> None:
        """
        Unload the TicketMod.
        """
        self.cancel()

    def cancel(self) -> None:
        """
        Cancel TicketMod scheduled tasks.
        """
        task = self.prompt_task
        if task and not task.cancelled() and not task.done():
            task.cancel()

    def get_current_ticket(self) -> Optional[Ticket]:
        # Get current ticket
        ticket = fetch_tickets_where(
            modid=self.modid,
            stage=[TicketStage.DELIVERED, TicketStage.NEW],
            _extra="ORDER BY stage DESC, id ASC LIMIT 1")
        return next(ticket, None)

    async def schedule_prompt(self) -> None:
        """
        Schedule or reschedule the reminder prompt.
        """
        # Cancel the existing task, if it exists
        if self.prompt_task and not self.prompt_task.cancelled():
            self.prompt_task.cancel()

        # Schedule the next prompt
        self.prompt_task = asyncio.create_task(self.prompt())

    async def prompt(self) -> None:
        """
        Prompt the moderator to provide a comment for the most recent ticket.
        """
        if not self.current_ticket: return

        if (msgid := self.last_prompt_msgid):
            # Wait until the next prompt is due
            next_prompt_at = discord.Object(msgid).created_at.timestamp() + self.prompt_interval
            try:
                await asyncio.sleep(next_prompt_at - datetime.datetime.utcnow().timestamp())
            except asyncio.CancelledError:
                return

        user = await self.find_user()
        if msgid and msgid != self.current_ticket.delivered_id:
            # Delete last prompt
            try:
                old_prompt = await user.fetch_message(msgid)
                await old_prompt.delete()
            except discord.HTTPException:
                pass
        # Send new prompt
        try:
            ticket_msg = await self.get_ticket_message()
            prompt_msg = await user.send("Please comment on the above!", reference=ticket_msg)
            self.last_prompt_msgid = prompt_msg.id
        except discord.HTTPException:
            self.last_prompt_msgid = None

        # Schedule the next reminder task
        self.prompt_task = asyncio.create_task(self.prompt())

    async def ticket_updated(self, ticket: Ticket) -> None:
        """
        Processes a ticket update.
        """
        if ticket.modid != self.modid:
            # This should never happen
            return

        if not self.current_ticket:
            # If we don't have a current ticket, this must be a new ticket
            await self.deliver()
        elif self.current_ticket.id == ticket.id:
            if not self.current_ticket.delivered_id:
                await self.deliver()
            else:
                # Assume the current ticket has been updated
                # Update the current ticket message
                self.current_ticket = ticket
                args: Dict[str, Any] = {'embed': ticket.embed}
                if ticket.stage == TicketStage.COMMENTED:
                    args['content'] = None
                ticket_msg = await self.get_ticket_message()
                if ticket_msg:
                    await ticket_msg.edit(**args)

    async def ticket_removed(self, ticket: Ticket, reason: Optional[str] = None) -> None:
        """
        Processes a removed ticket, with optional reason given.
        """
        if self.current_ticket and self.current_ticket.id == ticket.id:
            # Post the reason
            user = await self.fetch_user()
            await user.send(reason or "Ticket #{} was removed from your queue!".format(ticket.id))

            # Deliver next ticket
            await self.deliver()

    async def deliver(self) -> None:
        """
        Deliver the current ticket and refresh the prompt.
        """
        # TODO: Scheduling logic to handle delivery failure
        # TODO: Logic to handle non-existent user
        self.current_ticket = self.get_current_ticket()
        if self.current_ticket:
            logger.debug("Delivering ticket #{} to mod {}".format(self.current_ticket.id, self.modid))
            try:
                user = await self.find_user()
                self.current_msg = await user.send(
                    content="Please comment on the following:",
                    embed=self.current_ticket.embed)
            except discord.HTTPException:
                # Reschedule
                pass
            else:
                # Set current ticket to being delivered
                self.current_ticket.update(stage=TicketStage.DELIVERED, delivered_id=self.current_msg.id)

                # Update the last prompt message
                self.last_prompt_msgid = self.current_msg.id

                # (Re-)schedule the next prompt update
                await self.schedule_prompt()

    async def process_message(self, message: discord.Message) -> None:
        """
        Process a non-command message from the moderator.
        If there is a current active ticket, treat it as a comment.
        Either way, update the last handled message in data.
        """
        prefix = plugins.commands.conf.prefix
        if not prefix or not message.content.startswith(prefix):
            content = message.content
            if ticket := self.current_ticket:
                logger.info(
                    "Processing message from moderator {} as comment to ticket #{}: {}".format(
                        self.modid, ticket.id, repr(content)))

                # Parse the message as a comment to the current ticket
                duration, comment, msg = parse_ticket_comment(ticket, content)

                # Update the ticket
                with ticket.batch_update():
                    ticket.stage = TicketStage.COMMENTED
                    ticket.comment = comment
                    ticket.modified_by = self.modid
                    ticket.duration = duration
                    if ticket.status == TicketStatus.NEW:
                        ticket.status = TicketStatus.IN_EFFECT

                self.last_read_msgid = message.id

                user = await self.find_user()
                await user.send("Ticket comment set! " + msg)

                # Publish the ticket
                # Implicitly triggers update of the last ticket message
                await ticket.publish()

                # Deliver the next ticket
                await self.deliver()
            else:
                self.last_read_msgid = message.id

async def reload_mods() -> None:
    """
    Reload all moderators from data.
    """
    global ticketmods
    logger.debug("Loading ticket moderators.")

    # Unload mods
    for mod in ticketmods.values():
        mod.unload()

    # Rebuild ticketmod list
    ticketmods = {row[0]: TicketMod(row) for row in TicketMod._select_where()}

    # Load mods
    for mod in ticketmods.values():
        await mod.load()

    logger.info("Loaded {} ticket moderators.".format(len(ticketmods)))


def get_or_create_mod(modid: int) -> TicketMod:
    """
    Get a single TicketMod by modid, or create it if it doesn't exist.
    """
    mod = ticketmods.get(modid, None)
    if not mod:
        mod = TicketMod(TicketMod._insert(modid=modid))
        ticketmods[modid] = mod
    return mod

# ------------ Commands ------------

def resolve_ticket(msg: discord.Message, args: plugins.commands.ArgParser) -> Optional[Ticket]:
    """
    Resolves a ticket from the given message and command args, if possible.
    Ticket is extracted from either the referenced message or the first arg.
    """
    ticket = None
    if ref := msg.reference:
        if (ref_msg := ref.resolved) and isinstance(ref_msg, discord.Message):
            if ref_msg.author == discord_client.client.user and ref_msg.embeds:
                embed = ref_msg.embeds[0]
                name = embed.author.name
                if isinstance(name, str) and name.startswith("Ticket #"):
                    ticket_id = int(name[8:].split(' ', maxsplit=1)[0])
                    ticket = get_ticket(ticket_id)
    if ticket is None:
        ticketarg = args.next_arg()
        if ticketarg is not None and isinstance(ticketarg, plugins.commands.StringArg):
            maybe_id = int(ticketarg.text)
            # This is either a message snowflake (a big number) or a ticket id (small number). The leading 42 bits of a
            # snowflake are the timestamp and we assume that if all of those are zero, it's probably not a snowflake as
            # that would imply an epoch time of 0 milliseconds.
            if maybe_id < 2**(10+12):
                tickets = fetch_tickets_where(id=maybe_id)
            else:
                tickets = fetch_tickets_where(list_msgid=maybe_id)
            ticket = next(tickets, None)
    return ticket

def summarise_tickets(*tickets: Ticket, title: str = "Tickets", fmt: Optional[str] = None
    ) -> Optional[Iterator[discord.Embed]]:
    """
    Create paged embeds of ticket summaries from the provided list of tickets.
    """
    if not tickets:
        return None

    lines = [ticket.summary(fmt=fmt) for ticket in tickets]
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
    with plugins.reactions.ReactionMonitor(channel_id=msg.channel.id, message_id=msg.id, event='add',
        filter=lambda _, p: p.emoji.name in reactions, timeout_each=120) as mon:
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


@plugins.commands.command("note")
@plugins.privileges.priv("mod")
async def cmd_note(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    """
    Create a note on the target user.
    """
    if not isinstance(target_arg := args.next_arg(), plugins.commands.UserMentionArg):
        # TODO: Usage
        return
    targetid = target_arg.id

    note = args.get_rest()
    if not note:
        # Request the note dynamically
        prompt = await msg.channel.send("Please enter the note:")
        del_reaction = '\u274C'
        await prompt.add_reaction(del_reaction)
        with plugins.reactions.ReactionMonitor(channel_id=msg.channel.id, message_id=prompt.id, author_id=msg.author.id,
            event="add", filter=lambda _, p: p.emoji.name == del_reaction) as mon:
            msg_task = asyncio.create_task(
                discord_client.client.wait_for('message',
                    check=lambda msg_: msg_.channel == msg.channel and msg_.author == msg.author))
            reaction_task = asyncio.ensure_future(mon)
            try:
                done, pending = await asyncio.wait((msg_task, reaction_task),
                    timeout=300, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.TimeoutError:
                await msg.channel.send("Note prompt timed out, please try again.")

            if msg_task in done:
                note = msg_task.result().content
            elif reaction_task in done:
                await msg.channel.send("Note prompt cancelled, no note was created.")
            msg_task.cancel()
            reaction_task.cancel()

    if note:
        # Create the note ticket
        ticket = await create_ticket(
            type=TicketType.NOTE,
            modid=msg.author.id,
            targetid=targetid,
            created_at=datetime.datetime.utcnow(),
            created_by=msg.author.id,
            stage=TicketStage.COMMENTED,
            status=TicketStatus.IN_EFFECT,
            comment=note)

        # Ack note creation
        await msg.channel.send(embed=discord.Embed(
            description="[#{}]({}): Note created!".format(ticket.id, ticket.jump_link)))


@plugins.commands.command("tickets")
@plugins.commands.command("ticket")
@plugins.privileges.priv("mod")
async def cmd_ticket(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    user = msg.author
    reply = msg.channel.send
    no_mentions = discord.AllowedMentions.none()

    S_Arg = plugins.commands.StringArg
    UM_Arg = plugins.commands.UserMentionArg

    tickets: Iterable[Ticket]
    embeds: Optional[Iterable[discord.Embed]]

    cmd_arg = args.next_arg()
    if not isinstance(cmd_arg, S_Arg):
        return
    cmd = cmd_arg.text.lower()

    if cmd == "top":
        """
        Usage: ticket top
        DM you the ticket at the top of your queue (if any).
        Re-deliver the ticket at the top of your queue to your DMS.
        """
        mod = get_or_create_mod(user.id)
        if not mod.current_ticket:
            await reply("Your queue is empty, good job!")
        else:
            await mod.deliver()
            if msg.channel.type != discord.ChannelType.private:
                await reply("Ticket #{} has been delivered to your DMs.".format(mod.current_ticket.id))
    elif cmd == "queue":
        """
        Usage: ticket queue [modmention]
        Show the specified moderator's (or your own) ticket queue.
        """
        modarg = args.next_arg()
        if modarg is None or isinstance(modarg, UM_Arg):
            modid = modarg.id if modarg is not None else user.id
            embeds = None
            if modid in ticketmods:
                mod = ticketmods[modid]
                tickets = mod.queue

                embeds = summarise_tickets(
                    *tickets, title='Queue for {}'.format(modid),
                    fmt= "[#{id}]({jump_link}): ({status}) **{type}** for {targetid!m}")

            if embeds:
                await pager(msg.channel, [Page(embed=embed) for embed in embeds])
            else:
                await reply(util.discord.format("{!m} has an empty queue!", modid),
                    allowed_mentions=no_mentions)
    elif cmd == "take":
        """
        Usage: ticket take <ticket>
        Claim a ticket (i.e. set the responsible moderator to yourself).
        """
        if not (ticket := resolve_ticket(msg, args)):
            await reply("No ticket referenced or ticket could not be found.")
        elif ticket.modid == msg.author.id:
            await reply("This is already your ticket!")
        else:
            ticket.update(modid=msg.author.id)
            await ticket.mod.ticket_removed(ticket,
                "Ticket #{} has been claimed by {}.".format(ticket.id, msg.author.mention))
            await ticket.publish()
            await reply("You have claimed ticket #{}.".format(ticket.id))
    elif cmd == "assign":
        """
        Usage: ticket assign <ticket> <modmention>
        Assign the specified ticket to the specified moderator.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        elif not isinstance((mod_arg := args.next_arg()), UM_Arg):
            await reply("Please provide a moderator mention!")
        else:
            if mod_arg.id == ticket.modid:
                await reply(util.discord.format("Ticket #{} is already assigned to {!m}", ticket.id, mod_arg.id),
                    allowed_mentions=no_mentions)
            else:
                old_mod = ticket.mod
                new_mod = get_or_create_mod(mod_arg.id)
                with ticket.batch_update():
                    ticket.modid = new_mod.modid
                    if ticket.stage != TicketStage.COMMENTED:
                        ticket.delivered_id = None
                        ticket.stage = TicketStage.NEW
                await old_mod.ticket_removed(ticket,
                    reason=util.discord.format("Ticket {}# has been claimed by {!m}!", ticket.id, new_mod.modid))
                await ticket.publish()
    elif cmd == "set":
        """
        Set or reset the duration and comment for a ticket.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        else:
            duration, comment, note = parse_ticket_comment(ticket, args.get_rest())

            # Update the ticket
            with ticket.batch_update():
                if comment:
                    ticket.comment = comment
                    note = "Ticket comment set! " + note
                ticket.modified_by = msg.author.id
                ticket.duration = duration

            await ticket.publish()
            await reply(embed=discord.Embed(description="[#{}]({}): {}".format(ticket.id, ticket.jump_link, note)))
    elif cmd == "append":
        """
        Append to the ticket reason.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        elif not (text := args.get_rest()):
            # TODO: Usage
            pass
        elif len(ticket.comment or "") + len(text) > 2000:
            await reply("Cannot append, exceeds maximum comment length!")
        else:
            with ticket.batch_update():
                if ticket.comment is None:
                    ticket.comment = text
                else:
                    ticket.comment = ticket.comment + '\n' + text
                ticket.modified_by = msg.author.id
            await ticket.publish()
            await reply(
                embed=discord.Embed(description="[#{}]({}): Ticket updated.".format(ticket.id, ticket.jump_link))
            )
    elif cmd == "revert":
        """
        Manually revert a ticket.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        elif not ticket.can_revert:
            await reply("This ticket type ({}) cannot be reverted!".format(ticket.title))
        elif not ticket.active:
            await reply(embed=discord.Embed(
                description=("[#{}]({}): Cannot be reverted as it is no longer active!".format(
                    ticket.id, ticket.jump_link))))
        else:
            await ticket.manual_revert(msg.author.id)
            await reply(embed=discord.Embed(
                description="[#{}]({}): Ticket reverted.".format(ticket.id, ticket.jump_link)))
    elif cmd == "hide":
        """
        Hide (and revert) a ticket.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        elif ticket.hidden:
            await reply(embed=discord.Embed(description="#{}: Is already hidden!".format(ticket.id)))
        else:
            reason = args.get_rest() or None
            await ticket.hide(msg.author.id, reason=reason)
            await reply(embed=discord.Embed(description="#{}: Ticket hidden.".format(ticket.id)))
    elif cmd == "show":
        """
        Show ticket(s) by ticketid or userid
        """
        arg = args.next_arg()
        if isinstance(arg, UM_Arg):
            # Collect tickets for the mentioned user
            userid = arg.id

            tickets = sorted(fetch_tickets_where(targetid=userid),
                key=lambda t: t.id, reverse=True)
            shown = []
            hidden = []
            for ticket in tickets:
                if ticket.hidden:
                    hidden.append(ticket)
                else:
                    shown.append(ticket)

            embeds = summarise_tickets(*shown, title='Tickets for {}'.format(userid),
                fmt="[#{id}]({jump_link}): ({status}) **{type}** by {modid!m}")
            hidden_field = ', '.join('#{}'.format(ticket.id) for ticket in hidden)

            if hidden_field:
                embeds = embeds or (discord.Embed(title='Tickets for {}'.format(userid)),)
                embeds = (embed.add_field(name="Hidden", value=hidden_field) for embed in embeds)

            if embeds:
                await pager(msg.channel, [Page(embed=embed) for embed in embeds])
            else:
                await reply("No tickets found for this user.")
        elif isinstance(arg, S_Arg) and arg.text.isdigit():
            # Assume provided number is a ticket id
            if ticket := get_ticket(int(arg.text)):
                await reply(embed=ticket.embed)
            else:
                await reply("No tickets found with this id!")
    elif cmd == "showhidden":
        """
        Show hidden ticket(s) by ticketid or userid
        """
        arg = args.next_arg()
        if isinstance(arg, UM_Arg):
            # Collect hidden tickets for the mentioned user
            userid = arg.id
            tickets = fetch_tickets_where(status=TicketStatus.HIDDEN, targetid=userid)
            embeds = summarise_tickets(*tickets, title='Hidden tickets for {}'.format(userid),
                fmt="#{id}: **{type}** by {modid!m}")

            if embeds:
                await pager(msg.channel, [Page(embed=embed) for embed in embeds])
            else:
                await reply("No hidden tickets found for this user.")
        elif isinstance(arg, S_Arg) and arg.text.isdigit():
            # Assume provided number is a ticket id
            if ticket := get_ticket(int(arg.text)):
                await reply(embed=ticket.embed)
            else:
                await reply("No tickets found with this id!")
    elif cmd == "history":
        """
        Show revision history for a given ticket
        """
        pass
    else:
        pass


# ------------ Event handlers ------------

util.discord.event("member_ban")(update_audit_log)
util.discord.event("member_kick")(update_audit_log)

@util.discord.event("voice_state_update")
async def process_voice_state(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if before.deaf != after.deaf or before.mute != after.mute:
        await update_audit_log()
    if after.channel is not None:
        if member.id in conf.pending_unmutes:
            try:
                await member.edit(mute=False)
                conf.pending_unmutes = util.frozen_list.FrozenList(
                    filter(lambda i: i != member.id, conf.pending_unmutes))
                logging.debug("Processed unmute for {}".format(member.id))
            except discord.HTTPException as exc:
                if exc.text != "Target user is not connected to voice.":
                    raise
        if member.id in conf.pending_undeafens:
            try:
                await member.edit(deafen=False)
                conf.pending_undeafens = util.frozen_list.FrozenList(
                    filter(lambda i: i != member.id, conf.pending_undeafens))
                logging.debug("Processed undeafen for {}".format(member.id))
            except discord.HTTPException as exc:
                if exc.text != "Target user is not connected to voice.":
                    raise

@util.discord.event("member_update")
async def process_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.roles != after.roles:
        await update_audit_log()

@util.discord.event("message")
async def moderator_message(message: discord.Message) -> None:
    if message.channel.type == discord.ChannelType.private:
        if message.author.id in ticketmods:
            await ticketmods[message.author.id].process_message(message)

# Initial loading
@util.asyncio.init_async
async def init_setup() -> None:
    # Wait until the caches have been populated
    await discord_client.client.wait_until_ready()

    if not conf.guild or not discord_client.client.get_guild(int(conf.guild)):
        """
        No guild, nothing we can do. Don't proceed with setup.
        """
        logger.error("Guild not configured, or can't find the configured guild! Aborting setup.")
        return
    # Reload the TicketMods
    await reload_mods()
    # Trigger a read of the audit log, catch up on anything we may have missed
    await update_audit_log()
