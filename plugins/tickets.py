import re
import itertools
import datetime as dt
import asyncio
import logging
import contextlib
from functools import reduce
from collections import namedtuple
from enum import Enum
from typing import List


from psycopg2.extensions import QuotedString
import discord

from discord_client import client
import util.db
import util.discord

import plugins.commands as commands
from plugins.reactions import ReactionMonitor
import plugins.privileges as priv


logger = logging.getLogger(__name__)

# ---------- Constants ----------
ticket_comment_re = re.compile(
    r"""
    (?i)\s*
    ([\d.]+)
    (s(?:ec(?:ond)?s?)?
    |(?-i:m)|min(?:ute)?s?
    |h(?:(?:ou)?rs?)?
    |d(?:ays?)?
    |w(?:(?:ee)?ks?)
    |(?-i:M)|months?
    |y(?:(?:ea)?rs?)?
    )
    |p(?:erm(?:anent)?)?\W+
    """, re.VERBOSE | re.IGNORECASE
)

time_expansion = {
    's': 1,
    'm': 60,
    'h': 60 * 60,
    'd': 60 * 60 * 24,
    'w': 60 * 60 * 24 * 7,
    'M': 60 * 60 * 24 * 30,
    'y': 60 * 60 * 24 * 365
}

# ----------- Config -----------
conf = util.db.kv.Config(__name__)  # General plugin configuration

conf.guild: str  # ID of the guild the ticket system is managing
conf.tracked_roles: List[str]  # List of roleids of tracked roles
conf.last_audit_id: str  # ID of last audit event processed
conf.ticket_list: str  # Channel id of the ticket list in the guild


# ----------- Data -----------
@util.db.init
def init():
    return r"""
        CREATE SCHEMA tickets;

        CREATE TYPE TicketType AS ENUM (
            'NOTE',
            'KICK',
            'BAN',
            'VC_MUTE',
            'VC_DEAFEN',
            'ADD_ROLE'
        );

        CREATE TYPE TicketStatus AS ENUM (
            'NEW',
            'IN_EFFECT',
            'EXPIRED',
            'REVERTED',
            'HIDDEN'
        );

        CREATE TYPE TicketStage AS ENUM (
            'NEW',
            'DELIVERED',
            'COMMENTED'
        );


        CREATE TABLE tickets.tickets (
            id            SERIAL        PRIMARY KEY,
            type          TicketType    NOT NULL,
            stage         TicketStage   NOT NULL,
            status        TicketStatus  NOT NULL,
            modid         BIGINT        NOT NULL,
            targetid      BIGINT        NOT NULL,
            roleid        BIGINT,
            auditid       BIGINT,
            duration      INT,
            comment       TEXT,
            list_msgid    BIGINT,
            delivered_id  BIGINT,
            created_at    TIMESTAMP,
            modified_by   BIGINT
        );

        CREATE TABLE tickets.mods (
            modid               BIGINT PRIMARY KEY,
            last_read_msgid     BIGINT,
            last_prompt_msgid   BIGINT
        );

        CREATE TABLE tickets.tracked_roles(
                roleid BIGINT PRIMARY KEY
        );

        CREATE TABLE tickets.history (
            version             INT,
            last_modified_at    TIMESTAMP,
            id                  INT,
            type                TicketType,
            stage               TicketStage,
            status              TicketStatus,
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


class fieldConstants(Enum):
    """
    A collection of database field constants to use for selection conditions.
    """
    NULL = "IS NULL"
    NOTNULL = "IS NOT NULL"


class _rowInterface:
    __slots__ = ('row', '_pending')

    _conn = util.db.connection()

    _table = None
    _id_col = None
    _columns = {}

    def __init__(self, row, *args, **kwargs):
        self.row = row
        self._pending = None

    def __repr__(self):

        return "{}({})".format(
            self.__class__.__name__,
            ', '.join("{}={!r}".format(col, self.row[i])
                      for i, col in enumerate(self._columns))
        )

    def __getattr__(self, key):
        if key in self._columns:
            if self._pending and key in self._pending:
                return self._pending[key]
            else:
                return self.row[self._columns.index(key)]
        else:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        if key in self._columns:
            if self._pending is None:
                self.update(**{key: value})
            else:
                self._pending[key] = value
        else:
            super().__setattr__(key, value)

    @contextlib.contextmanager
    def batch_update(self):
        if self._pending:
            raise ValueError(
                "Nested batch updates for {}!".format(
                    self.__class__.__name__
                )
            )

        self._pending = {}
        try:
            yield self._pending
        finally:
            self.update(**self._pending)
            self._pending = None

    def _refresh(self):
        rows = self._select_where(
            **{self._columns[self._id_col]: self.row[self._id_col]}
        )
        if not rows:
            raise ValueError(
                "Refreshing a {} which no longer exists!".format(
                    self.__class__.__name__
                )
            )
        self.row = rows[0]

    def update(self, **values):
        rows = self._update_where(
            values,
            **{self._columns[self._id_col]: self.row[self._id_col]}
        )
        if not rows:
            raise ValueError(
                "Updating a {} which no longer exists!".format(
                    self.__class__.__name__
                )
            )
        self.row = rows[0]

    @staticmethod
    def format_conditions(conditions):
        if not conditions:
            return ("", tuple())

        values = []
        conditional_strings = []
        for key, item in conditions.items():
            if isinstance(item, (list, tuple)):
                conditional_strings.append(
                    "{} IN ({})".format(key, ", ".join(['%s'] * len(item)))
                )
                values.extend(item)
            elif isinstance(item, fieldConstants):
                conditional_strings.append(
                    "{} {}".format(key, item.value)
                )
            else:
                conditional_strings.append("{}=%s".format(key))
                values.append(item)

        return (' AND '.join(conditional_strings), values)

    @classmethod
    def _select_where(cls, _extra=None, **conditions):
        with cls._conn as conn:
            with conn.cursor() as cursor:
                cond_str, cond_values = cls.format_conditions(conditions)

                cursor.execute(
                    "SELECT * FROM {} {} {} {}".format(
                        cls._table,
                        'WHERE' if conditions else '',
                        cond_str,
                        _extra or ''
                    ),
                    cond_values
                )
                return cursor.fetchall()

    @classmethod
    def _insert(cls, **values):
        with cls._conn as conn:
            with conn.cursor() as cursor:
                columns = ', '.join(values.keys())
                value_str = ', '.join('%s' for _ in values)
                values = tuple(values.values())

                cursor.execute(
                    "INSERT INTO {} ({}) VALUES ({}) RETURNING *".format(
                        cls._table,
                        columns,
                        value_str
                    ),
                    values
                )
                return cursor.fetchone()

    @classmethod
    def _update_where(cls, values, **conditions):
        with cls._conn as conn:
            with conn.cursor() as cursor:
                cond_str, cond_values = cls.format_conditions(conditions)
                value_str = ', '.join('{}=%s'.format(key)
                                      for key in values.keys())
                values = tuple(values.values())

                cursor.execute(
                    "UPDATE {} SET {} WHERE {} RETURNING *".format(
                        cls._table,
                        value_str,
                        cond_str
                    ),
                    (*values, *cond_values)
                )
                return cursor.fetchall()


# ----------- Tickets -----------

class FieldEnum(str, Enum):
    """
    String enum with description conforming to the ISQLQuote protocol.
    Allows processing by psycog
    """
    def __new__(cls, value, desc):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.desc = desc
        return obj

    def __repr__(self):
        return '<%s.%s>' % (self.__class__.__name__, self.name)

    def __bool__(self):
        return True

    def __conform__(self, proto):
        return QuotedString(self.value)


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


class Ticket(_rowInterface):
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

    title: str = None   # Friendly human readable title used for ticket embeds
    can_revert: bool = None  # Whether this ticket type can expire

    # Action triggering automatic ticket creation
    trigger_action: discord.AuditLogAction = None
    # Action triggering automatic ticket reversal
    revert_trigger_action: discord.AuditLogAction = None

    @property
    def embed(self) -> discord.Embed:
        """
        The discord embed describing this ticket.
        """
        embed = discord.Embed(
            title=self.title,
            description=self.comment or "No comment",
            timestamp=self.created_at
        ).set_author(
            name="Ticket #{} ({})".format(
                self.id,
                TicketStatus(self.status).desc
            )
        ).set_footer(
            text="Moderator: {}".format(self.mod.user or self.modid)
        ).add_field(
            name="Target",
            value=util.discord.format("{0!m}\n({0})", self.targetid)
        )

        if self.roleid:
            if (role := self.role):
                value = "{}\n({})".format(role.name, role.id)
            else:
                value = str(self.roleid)
            embed.add_field(
                name="Role",
                value=value
            )

        if self.duration:
            embed.add_field(
                name="Duration",
                value=str(dt.timedelta(seconds=self.duration))
            )
        return embed

    @property
    def history(self):
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
    def expiry(self) -> dt.datetime:
        """
        Expiry timestamp for this ticket, if applicable.
        """
        if self.can_revert and self.duration is not None:
            return self.created_at + dt.timedelta(seconds=self.duration)

    @property
    def mod(self):
        """
        TicketMod associated to this ticket.
        """
        return get_or_create_mod(self.modid)

    @property
    def target(self) -> discord.Member:
        return client.get_guild(int(conf.guild)).get_member(self.targetid)

    @property
    def role(self) -> discord.Role:
        return client.get_guild(int(conf.guild)).get_role(self.roleid)

    @property
    def jump_link(self) -> str:
        return 'https://discord.com/channels/{}/{}/{}'.format(
            conf.guild,
            conf.ticket_list,
            self.list_msgid
        )

    def summary(self, fmt=None) -> str:
        """
        A short one-line summary of the ticket.
        """
        fmt = fmt or ("[#{id}]({jump_link})(`{status:<9}`):"
                      " **{type}** for {targetid!m} by {modid!m}.")

        fmt_dict = {col: self.row[i] for i, col in enumerate(self._columns)}
        fmt_dict['status'] = TicketStatus(self.status).name
        fmt_dict['stage'] = TicketStage(self.stage).name
        fmt_dict['type'] = TicketType(self.type).name

        return util.discord.format(
            fmt,
            ticket=self,
            title=self.title,
            jump_link=self.jump_link,
            **fmt_dict
        )

    async def publish(self):
        """
        Ticket update hook.
        Should be run whenever a ticket is created or updated.
        Manages the ticket list embed.
        Defers to the expiry and ticket mod update hooks.
        """
        # Reschedule or cancel ticket expiry if required
        update_expiry_for(self)

        # Post to or update the ticket list
        if conf.ticket_list:
            channel = client.get_channel(int(conf.ticket_list))
            if channel:
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
                            pass
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
    def _create(cls, **kwargs):
        """
        Creates a new ticket from the given `kwargs`.
        The `kwargs` must be a collection of column/value pairs to insert.
        """
        row = cls._insert(**kwargs)
        ticket = cls(row)
        logger.debug("Ticket created: {!r}".format(ticket))
        return ticket

    @classmethod
    async def create_from_audit(cls, audit_entry):
        """
        Handle a *creation* audit entry.
        Create a new ticket from the entry data if required.
        """
        raise NotImplementedError

    @classmethod
    async def revert_from_audit(cls, audit_entry):
        """
        Handle a *revert* audit entry.
        Revert a ticket from the entry data if required.
        """
        raise NotImplementedError

    async def _revert_action(self, reason=None):
        """
        Attempt to reverse the ticket moderation action.
        Transparently re-raise exceptions.
        """
        raise NotImplementedError
        pass

    async def expire(self, **kwargs):
        """
        Automatically expire the ticket.
        """
        # TODO: Expiry error handling
        result = await self._revert_action(
            reason="Ticket #{}: Automatic expiry.".format(self.id)
        )
        if result:
            self.update(
                status=TicketStatus.EXPIRED,
                modified_by=0
            )
            await self.publish()

    async def manual_revert(self, actorid: int, **kwargs):
        """
        Manually revert the ticket.
        """
        result = await self._revert_action(
            reason="Ticket #{}: Moderator {} requested revert.".format(
                self.id,
                actorid
            )
        )
        if result:
            self.update(
                status=TicketStatus.REVERTED,
                modified_by=actorid
            )
            await self.publish()
        return result

    async def hide(self, actorid: int, reason=None, **kwargs):
        """
        Revert a ticket and set its status to HIDDEN.
        """
        result = await self._revert_action(
            reason="Ticket #{}: Moderator {} hid the ticket.".format(
                self.id,
                actorid
            )
        )
        if result:
            with self.batch_update():
                self.status = TicketStatus.HIDDEN
                self.modified_by = actorid
                if reason is not None:
                    self.comment = self.comment + '\n' + reason
            await self.publish()
        return result


# Map of ticket types to the associated class.
_ticket_types = {}
# Map of audit actions to the associated handler methods.
_action_handlers = {}


# Decorator to register Ticket subclasses for each TicketType
def _ticket_type(cls):
    _ticket_types[cls._type] = cls
    if (action := cls.trigger_action) is not None:
        if action in _action_handlers:
            _action_handlers[action].append(cls.create_from_audit)
        else:
            _action_handlers[action] = [cls.create_from_audit]

    if (action := cls.revert_trigger_action) is not None:
        if action in _action_handlers:
            _action_handlers[action].append(cls.revert_from_audit)
        else:
            _action_handlers[action] = [cls.revert_from_audit]


@_ticket_type
class NoteTicket(Ticket):
    _type = TicketType.NOTE

    title = "Note"
    can_revert = True

    trigger_action = None
    revert_trigger_action = None

    async def _revert_action(self, reason=None):
        """
        Notes have no revert action
        """
        return True

    async def manual_revert(self, modified_by, **kwargs):
        """
        Manually reverted notes are hidden.
        """
        self.update(
            status=TicketStatus.HIDDEN,
            modified_by=modified_by.id
        )
        await self.publish()

    async def expire(self, **kwargs):
        """
        Expiring notes are hidden.
        """
        self.update(
            status=TicketStatus.HIDDEN,
            modified_by=0
        )
        await self.publish()


@_ticket_type
class KickTicket(Ticket):
    _type = TicketType.KICK

    title = "Kick"
    can_revert = False

    trigger_action = discord.AuditLogAction.kick
    revert_trigger_action = None

    @classmethod
    async def create_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a kick audit event.
        """
        await cls._create(
            type=cls._type,
            stage=TicketStage.NEW,
            status=TicketStatus.NEW,
            modid=audit_entry.user.id,
            targetid=audit_entry.target.id,
            auditid=audit_entry.id,
            roleid=None,
            created_at=audit_entry.created_at,
            modified_by=0,
            comment=audit_entry.reason
        ).publish()


@_ticket_type
class BanTicket(Ticket):
    _type = TicketType.BAN

    title = "Ban"
    can_revert = True

    trigger_action = discord.AuditLogAction.ban
    revert_trigger_action = discord.AuditLogAction.unban

    @classmethod
    async def create_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a ban audit event.
        """
        await cls._create(
            type=cls._type,
            stage=TicketStage.NEW,
            status=TicketStatus.NEW,
            modid=audit_entry.user.id,
            targetid=audit_entry.target.id,
            auditid=audit_entry.id,
            roleid=None,
            created_at=audit_entry.created_at,
            modified_by=0,
            comment=audit_entry.reason
        ).publish()

    @classmethod
    async def revert_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle an unban audit event.
        """
        # Select any relevant tickets
        tickets = fetch_tickets_where(
            type=cls._type,
            targetid=audit_entry.target.id,
            status=[TicketStatus.NEW, TicketStatus.IN_EFFECT]
        )
        for ticket in tickets:
            ticket.update(
                status=TicketStatus.REVERTED,
                modified_by=audit_entry.user.id
            )
            await ticket.publish()

    async def _revert_action(self, reason=None):
        """
        Unban the acted user, if possible.
        """
        guild = client.get_guild(int(conf.guild))
        bans = await guild.bans()
        user = next(
            (entry.user for entry in bans if entry.user.id == self.targetid),
            None
        )
        if user is None:
            # User is not banned, nothing to do
            return True
        await guild.unban(user, reason=reason)
        return True


@_ticket_type
class VCMuteTicket(Ticket):
    _type = TicketType.VC_MUTE

    title = "VC Mute"
    can_revert = True

    trigger_action = discord.AuditLogAction.member_update
    revert_trigger_action = discord.AuditLogAction.member_update

    @classmethod
    async def create_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a VC mute event.
        """
        if not hasattr(audit_entry.before, "mute"):
            return
        if not audit_entry.before.mute and audit_entry.after.mute:
            await cls._create(
                type=cls._type,
                stage=TicketStage.NEW,
                status=TicketStatus.NEW,
                modid=audit_entry.user.id,
                targetid=audit_entry.target.id,
                auditid=audit_entry.id,
                roleid=None,
                created_at=audit_entry.created_at,
                modified_by=0,
                comment=audit_entry.reason
            ).publish()

    @classmethod
    async def revert_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a VC unmute event
        """
        if not hasattr(audit_entry.before, "mute"):
            return
        if audit_entry.before.mute and not audit_entry.after.mute:
            # Select any relevant tickets
            tickets = fetch_tickets_where(
                type=cls._type,
                targetid=audit_entry.target.id,
                status=[TicketStatus.NEW, TicketStatus.IN_EFFECT]
            )
            for ticket in tickets:
                ticket.update(
                    status=TicketStatus.REVERTED,
                    modified_by=audit_entry.user.id
                )
                await ticket.publish()

    async def _revert_action(self, reason=None):
        """
        Attempt to unmute the target user.
        """
        guild = client.get_guild(int(conf.guild))
        member = guild.get_member(self.targetid)
        if member is None:
            # User is no longer in the guild, nothing to do
            return True
        await member.edit(mute=True)


@_ticket_type
class VCDeafenTicket(Ticket):
    _type = TicketType.VC_DEAFEN

    title = "VC Deafen"
    can_revert = True

    trigger_action = discord.AuditLogAction.member_update
    revert_trigger_action = discord.AuditLogAction.member_update

    @classmethod
    async def create_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a VC deafen event.
        """
        if not hasattr(audit_entry.before, "deaf"):
            return
        if not audit_entry.before.deaf and audit_entry.after.deaf:
            await cls._create(
                type=cls._type,
                stage=TicketStage.NEW,
                status=TicketStatus.NEW,
                modid=audit_entry.user.id,
                targetid=audit_entry.target.id,
                auditid=audit_entry.id,
                roleid=None,
                created_at=audit_entry.created_at,
                modified_by=0,
                comment=audit_entry.reason
            ).publish()

    @classmethod
    async def revert_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a VC undeafen event
        """
        if not hasattr(audit_entry.before, "deaf"):
            return
        if audit_entry.before.deaf and not audit_entry.after.deaf:
            # Select any relevant tickets
            tickets = fetch_tickets_where(
                type=cls._type,
                targetid=audit_entry.target.id,
                status=[TicketStatus.NEW, TicketStatus.IN_EFFECT]
            )
            for ticket in tickets:
                ticket.update(
                    status=TicketStatus.REVERTED,
                    modified_by=audit_entry.user.id
                )
                await ticket.publish()

    async def _revert_action(self, reason=None):
        """
        Attempt to undeafen the target user.
        """
        guild = client.get_guild(int(conf.guild))
        member = guild.get_member(self.targetid)
        if member is None:
            # User is no longer in the guild, nothing to do
            return True
        await member.edit(deafen=True)


@_ticket_type
class AddRoleTicket(Ticket):
    _type = TicketType.ADD_ROLE

    title = "Role Added"
    can_revert = True

    trigger_action = discord.AuditLogAction.member_role_update
    revert_trigger_action = discord.AuditLogAction.member_role_update

    @classmethod
    async def create_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a tracked role add event.
        """
        if audit_entry.changes.after.roles:
            for role in audit_entry.changes.after.roles:
                if conf.tracked_roles and str(role.id) in conf.tracked_roles:
                    await cls._create(
                        type=cls._type,
                        stage=TicketStage.NEW,
                        status=TicketStatus.NEW,
                        modid=audit_entry.user.id,
                        targetid=audit_entry.target.id,
                        auditid=audit_entry.id,
                        roleid=role.id,
                        created_at=audit_entry.created_at,
                        modified_by=0,
                        comment=audit_entry.reason
                    ).publish()

    @classmethod
    async def revert_from_audit(cls, audit_entry: discord.AuditLogEntry):
        """
        Handle a tracked role remove event.
        """
        if audit_entry.changes.before.roles:
            for role in audit_entry.changes.before.roles:
                if conf.tracked_roles and str(role.id) in conf.tracked_roles:
                    # Select any relevant tickets
                    tickets = fetch_tickets_where(
                        type=cls._type,
                        targetid=audit_entry.target.id,
                        roleid=role.id,
                        status=[TicketStatus.NEW, TicketStatus.IN_EFFECT]
                    )
                    for ticket in tickets:
                        ticket.update(
                            status=TicketStatus.REVERTED,
                            modified_by=audit_entry.user.id
                        )
                        await ticket.publish()

    async def _revert_action(self, reason=None):
        """
        Attempt to remove the associated role from the target.
        """
        guild = client.get_guild(int(conf.guild))
        role = guild.get_role(self.roleid)
        if role is None:
            return False
        target = guild.get_member(self.targetid)
        if target is None:
            return None
        await target.remove_roles(role)
        return True


async def read_audit_log(*args):
    """
    Read the audit log from the last read value and process new audit events.
    If there is no last read value, just reads the last value.
    """
    # TODO: Lock so we don't read simultaneously
    if not conf.guild or not (guild := client.get_guild(int(conf.guild))):
        """
        Nothing we can do
        """
        logger.error(
            "Guild not configured, or can't find the configured guild! "
            "Cannot read audit log."
        )
        return

    logger.debug("Reading audit entries since {}".format(conf.last_auditid))
    if conf.last_auditid:
        entries = [
            await guild.audit_logs(limit=100, oldest_first=True).flatten()
        ]
        # If there is more than one page of new entries, keep collecting them
        while entries[-1][0].id > int(conf.last_auditid):
            new_entries = await guild.audit_logs(
                limit=100,
                before=entries[0],
                oldest_first=True
            ).flatten()

            if not new_entries:
                break
            entries.append(new_entries)
        entries = filter(
            lambda entry: entry.id > int(conf.last_auditid),
            itertools.chain.from_iterable(reversed(entries))
        )
    else:
        # With no know last auditid, just read the last entry
        entries = await guild.audit_logs(limit=1).flatten()

    # Process each audit entry
    for entry in entries:
        logger.debug("Processing audit entry {}".format(entry))
        if entry.user != client.user and entry.action in _action_handlers:
            for handler in _action_handlers[entry.action]:
                await handler(entry)
        conf.last_auditid = str(entry.id)


def fetch_tickets_where(**kwargs):
    """
    Fetch Tickets matching the given conditions.
    Values must be given in data-compatible form.
    Lists of values are supported and will be converted to `IN` conditionals.
    """
    rows = Ticket._select_where(**kwargs)
    return (
        (_ticket_types[TicketType(row[Ticket._columns.index('type')])])(row)
        for row in rows
    )


async def create_ticket(type: TicketType, modid: int, targetid: int,
                        created_at: dt.datetime, created_by: int,
                        stage: TicketStage = None, status: TicketStatus = None,
                        auditid: int = None, roleid: int = None,
                        comment: str = None, duration: int = None):
    # Get the appropriate Ticket subclass
    TicketClass = _ticket_types[type]

    # Create and publish the ticket
    ticket = TicketClass._create(
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
        comment=comment
    )
    await ticket.publish()

    return ticket


def get_ticket(ticketid):
    tickets = fetch_tickets_where(id=ticketid)
    return next(tickets, None)


# ----------- Ticket expiry system -----------
_expiring_tickets = {}
_next_expiring = None
_expiry_task = None

_refresh_event = asyncio.Event()


async def _expire_next():
    global _expiry_task
    global _next_expiring

    # Sleep until the next ticket is ready to expire
    logger.debug(
        "Waiting for Ticket #{} to expire. (Expires at {})".format(
            _next_expiring[0],
            _next_expiring[1]
        )
    )
    try:
        await asyncio.sleep(
            _next_expiring[1].timestamp() - dt.datetime.utcnow().timestamp()
        )
    except asyncio.CancelledError:
        return

    # Retrieve the ticket and expire it
    ticketid = _next_expiring[0]
    ticket = get_ticket(ticketid)
    asyncio.create_task(ticket.expire())
    _expiring_tickets.pop(ticketid)

    # Schedule the next expiry
    if _expiring_tickets:
        _next_expiring = min(_expiring_tickets.items(), key=lambda p: p[1])
        _expiry_task = asyncio.create_task(_expire_next())


def _reload_expiration():
    global _expiry_task
    global _next_expiring

    if _expiring_tickets:
        # Get next ticket
        new_next = min(_expiring_tickets.items(), key=lambda p: p[1])
        if new_next != _next_expiring:
            if _expiry_task is not None:
                _expiry_task.cancel()
            _next_expiring = new_next
            _expiry_task = asyncio.create_task(_expire_next())


def update_expiry_for(ticket):
    if ticket.active and ticket.duration:
        logger.debug("Scheduling expiry for Ticket #{}.".format(ticket.id))
        # Save ticket expiry
        _expiring_tickets[ticket.id] = ticket.expiry

        # Regenerate next expiry
        _reload_expiration()
    else:
        if ticket.id in _expiring_tickets:
            logger.debug("Cancelling expiry for Ticket #{}.".format(ticket.id))
            _expiring_tickets.pop(ticket.id)
            _reload_expiration()


def init_ticket_expiry():
    """
    Refresh all ticket expiries from the database.
    """
    global _expiring_tickets

    expiring_tickets = fetch_tickets_where(
        status=[TicketStatus.NEW, TicketStatus.IN_EFFECT],
        duration=fieldConstants.NOTNULL,
    )
    _expiring_tickets = {
        ticket.id: ticket.expiry for ticket in expiring_tickets
    }
    logger.info("Loaded {} expiring tickets.".format(len(_expiring_tickets)))
    _reload_expiration()


# ----------- Ticket Mods and queue management -----------
_ticketmods = {}


class TicketMod(_rowInterface):
    __slots__ = (
        'current_ticket',
        '_prompt_task',
        '_delivery_task',
        '_current_msg'
    )

    _table = 'tickets.mods'
    _id_col = 0
    _columns = (
        'modid',
        'last_read_msgid',
        'last_prompt_msgid',
    )

    prompt_interval = 12 * 60 * 60

    def __init__(self, row):
        super().__init__(row)
        self.current_ticket = self.get_current_ticket()
        self._prompt_task = None
        self._delivery_task = None
        self._current_msg = None
        logger.debug(
            "Initialised ticket mod {}. Next ticket: {}".format(
                self.modid,
                self.current_ticket
            )
        )

    @property
    def queue(self):
        return fetch_tickets_where(
            modid=self.modid,
            stage=[TicketStage.DELIVERED, TicketStage.NEW],
            _extra="ORDER BY stage DESC, id ASC"
        )

    @property
    def user(self):
        """
        The Discord User object associated to this moderator.
        May be None if the user cannot be found.
        """
        return client.get_user(self.modid)

    async def get_ticket_message(self):
        """
        Get the current ticket delivery message in the DM, if it exists.
        """
        ticket = self.current_ticket
        if ticket and (msgid := ticket.delivered_id):
            if not self._current_msg or self._current_msg.id != msgid:
                # Update the cached message
                self._current_msg = await self.user.fetch_message(msgid)
            return self._current_msg

    async def load(self):
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
                last_read = discord.Object(
                    max(self.last_read_msgid or 0, ticket.delivered_id)
                )

                # Collect the missed messages
                mod_messages = []
                if self.user:
                    messages = await self.user.history(
                        after=last_read,
                        limit=None
                    ).flatten()
                    mod_messages = [
                        msg for msg in messages if msg.author.id == self.modid
                    ]

                if mod_messages:
                    logger.debug(
                        "Missed {} messages from moderator {}.".format(
                            len(mod_messages),
                            self.modid
                        )
                    )

                    # Process the first missed message
                    await self.process_message(mod_messages[0])
                    # Save the last missed message as the last one handled
                    if len(mod_messages) > 1:
                        self.last_read_msgid = mod_messages[-1].id
                else:
                    # Schedule the reminder prompt for the current ticket
                    await self.schedule_prompt()

    def unload(self):
        """
        Unload the TicketMod.
        """
        self.cancel()

    def cancel(self):
        """
        Cancel TicketMod scheduled tasks.
        """
        for task in (self._prompt_task, self._delivery_task):
            if task and not task.cancelled() and not task.done():
                task.cancel()

    def get_current_ticket(self) -> Ticket:
        # Get current ticket
        ticket = fetch_tickets_where(
            modid=self.modid,
            stage=[TicketStage.DELIVERED, TicketStage.NEW],
            _extra="ORDER BY stage DESC, id ASC LIMIT 1"
        )
        return next(ticket, None)

    async def schedule_prompt(self):
        """
        Schedule or reschedule the reminder prompt.
        """
        # Cancel the existing task, if it exists
        if self._prompt_task and not self._prompt_task.cancelled():
            self._prompt_task.cancel()

        # Schedule the next prompt
        self._prompt_task = asyncio.create_task(self._prompt())

    async def _prompt(self):
        """
        Prompt the moderator to provide a comment for the most recent ticket.
        """
        if (msgid := self.last_prompt_msgid):
            # Wait until the next prompt is due
            next_prompt_at = (discord.Object(msgid).created_at.timestamp()
                              + self.prompt_interval)
            try:
                await asyncio.sleep(
                    next_prompt_at - dt.datetime.utcnow().timestamp()
                )
            except asyncio.CancelledError:
                return

        user = self.user
        if user is not None:
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
                prompt_msg = await user.send(
                    "Please comment on the above!",
                    reference=ticket_msg
                )
                self.last_prompt_msgid = prompt_msg.id
            except discord.HTTPException:
                self.last_prompt_msgid = None

        # Schedule the next reminder task
        self._prompt_task = asyncio.create_task(self._prompt())

    async def ticket_updated(self, ticket):
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
                args = {'embed': ticket.embed}
                if ticket.stage == TicketStage.COMMENTED:
                    args['content'] = None
                await (await self.get_ticket_message()).edit(**args)

    async def ticket_removed(self, ticket, reason=None):
        """
        Processes a removed ticket, with optional reason given.
        """
        if self.current_ticket and self.current_ticket.id == ticket.id:
            # Post the reason
            await self.user.send(
                reason or
                "Ticket #{} was removed from your queue!".format(ticket.id)
            )

            # Deliver next ticket
            await self.deliver()

    async def deliver(self):
        """
        Deliver the current ticket and refresh the prompt.
        """
        # TODO: Scheduling logic to handle delivery failure
        # TODO: Logic to handle non-existent user
        self.current_ticket = self.get_current_ticket()
        if self.current_ticket:
            logger.debug(
                "Delivering ticket #{} to mod {}".format(
                    self.current_ticket.id,
                    self.modid
                )
            )
            try:
                self._current_msg = await self.user.send(
                    content="Please comment on the following:",
                    embed=self.current_ticket.embed
                )
            except discord.HTTPException:
                # Reschedule
                pass
            else:
                # Set current ticket to being delivered
                self.current_ticket.update(stage=TicketStage.DELIVERED,
                                           delivered_id=self._current_msg.id)

                # Update the last prompt message
                self.last_prompt_msgid = self._current_msg.id

                # (Re-)schedule the next prompt update
                await self.schedule_prompt()

    async def process_message(self, message):
        """
        Process a non-command message from the moderator.
        If there is a current active ticket, treat it as a comment.
        Either way, update the last handled message in data.
        """
        prefix = commands.conf.prefix
        if not prefix or not message.content.startswith(prefix):
            content = message.content
            if ticket := self.current_ticket:
                logger.info(
                    "Processing message from moderator {} "
                    "as comment to ticket #{}: {}".format(self.modid,
                                                          ticket.id,
                                                          repr(content))
                )

                # Parse the message as a comment to the current ticket
                if match := ticket_comment_re.match(content):
                    # Extract duration
                    if match[1]:
                        d = int(match[1])
                        token = match[2][0]
                        token = token.lower() if token != 'M' else token
                        duration = d * time_expansion[token]
                    else:
                        duration = None

                    # Extract comment
                    comment = content[match.end():]
                else:
                    duration = None
                    comment = content

                # Update the ticket
                with ticket.batch_update():
                    ticket.stage = TicketStage.COMMENTED
                    ticket.comment = comment
                    ticket.modified_by = self.modid
                    if ticket.can_revert and ticket.active:
                        ticket.duration = duration
                    if ticket.status == TicketStatus.NEW:
                        ticket.status = TicketStatus.IN_EFFECT

                self.last_read_msgid = message.id

                # Notify the moderator, nullify the duration if required
                msg = "Ticket comment set! "
                if duration:
                    if not ticket.can_revert:
                        msg += (
                            "Provided duration ignored since "
                            "this ticket type cannot expire."
                        )
                        duration = None
                    elif not ticket.active:
                        msg += (
                            "Provided duration ignored since "
                            "this ticket is no longer in effect."
                        )
                        duration = None
                    else:
                        expiry = ticket.expiry
                        now = dt.datetime.utcnow()
                        if expiry <= now:
                            msg += "Ticket will expire immediately!"
                        else:
                            msg += "Ticket will expire in {}.".format(
                                str(expiry - now).split('.')[0]
                            )

                await self.user.send(msg)

                # Publish the ticket
                # Implicitly triggers update of the last ticket message
                await self.current_ticket.publish()

                # Schedule ticket expiration, if required
                if duration is not None:
                    update_expiry_for(self.current_ticket)

                # Deliver the next ticket
                await self.deliver()
            else:
                self.last_read_msgid = message.id


async def reload_mods():
    """
    Reload all moderators from data.
    """
    global _ticketmods
    logger.debug("Loading ticket moderators.")

    # Unload mods
    for mod in _ticketmods.values():
        mod.unload()

    # Rebuild ticketmod list
    _ticketmods = {row[0]: TicketMod(row) for row in TicketMod._select_where()}

    # Load mods
    for mod in _ticketmods.values():
        await mod.load()

    logger.info("Loaded {} ticket moderators.".format(len(_ticketmods)))


def get_or_create_mod(modid) -> TicketMod:
    """
    Get a single TicketMod by modid, or create it if it doesn't exist.
    """
    mod = _ticketmods.get(modid, None)
    if not mod:
        mod = TicketMod(TicketMod._insert(modid=modid))
        _ticketmods[modid] = mod
    return mod


# ------------ Commands ------------

def resolve_ticket(msg, args, rewind=False) -> Ticket:
    """
    Resolves a ticket from the given message and command args, if possible.
    Ticket is extracted from either the referenced message or the first arg.
    """
    ticket = None
    if ref := msg.reference:
        if (ref_msg := ref.resolved) and isinstance(ref_msg, discord.Message):
            if ref_msg.author == client.user and ref_msg.embeds:
                embed = ref_msg.embeds[0]
                if (name := embed.author.name) and name.startswith("Ticket #"):
                    ticket_id = int(name[8:].split(' ', maxsplit=1)[0])
                    ticket = get_ticket(ticket_id)
    if ticket is None:
        rewind_pos = args.pos
        ticketarg = args.next_arg()
        if ticketarg is not None and isinstance(ticketarg, commands.StringArg):
            maybe_id = int(ticketarg.text)
            # This is either a message snowflake (a big number) or a ticket
            # id (small number). The leading 42 bits of a snowflake are the
            # timestamp and we assume that if all of those are zero, it's
            # probably not a snowflake as that would imply an epoch time of
            # 0 milliseconds.
            if maybe_id < 2**(10+12):
                tickets = fetch_tickets_where(id=maybe_id)
            else:
                tickets = fetch_tickets_where(list_msgid=maybe_id)
            ticket = next(tickets, None)
        if rewind and ticket is None:
            args.pos = rewind_pos
    return ticket


def summarise_tickets(*tickets, title="Tickets", fmt=None):
    """
    Create paged embeds of ticket summaries from the provided list of tickets.
    """
    if not tickets:
        return None

    lines = [ticket.summary(fmt=fmt) for ticket in tickets]
    blocks = ['\n'.join(lines[i:i+10]) for i in range(0, len(lines), 10)]
    page_count = len(blocks)

    embeds = (
        discord.Embed(description=blocks[i], title=title)
        for i in range(page_count)
    )

    if page_count > 1:
        embeds = (
            embed.set_footer(text="Page {}/{}".format(i+1, page_count))
            for i, embed in enumerate(embeds)
        )

    return embeds


Page = namedtuple('Page', ('content', 'embed'), defaults=(None, None))


async def pager(dest: discord.abc.Messageable, pages):
    """
    Page a sequence of pages.
    """
    _next_reaction = '⏭️'
    _prev_reaction = '⏮️'
    _all_reaction = '📜'
    reactions = (_prev_reaction, _all_reaction, _next_reaction)

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
    while True:
        try:
            with ReactionMonitor(
                message_id=msg.id,
                event='add',
                filter=lambda _, p: (
                    str(p.emoji) in reactions
                    and p.user_id != msg.guild.me.id
                ),
                timeout_each=120
            ) as mon:
                _, payload = await mon
                if str(payload.emoji) == _next_reaction:
                    index += 1
                elif str(payload.emoji) == _prev_reaction:
                    index -= 1
                elif str(payload.emoji) == _all_reaction:
                    await msg.delete()
                    msg = None
                    for page in pages:
                        await dest.send(**page._asdict())
                    break
                index %= len(pages)
                await msg.edit(**pages[index]._asdict())
                try:
                    await msg.remove_reaction(
                        payload.emoji,
                        discord.Object(payload.user_id)
                    )
                except discord.HTTPException:
                    pass
        except asyncio.TimeoutError:
            break
        except asyncio.CancelledError:
            break

    # Remove the reactions
    if msg is not None:
        try:
            for r in reactions:
                await msg.clear_reaction(r)
        except discord.HTTPException:
            pass


@commands.command("note")
@priv.priv("mod")
async def cmd_note(msg: discord.Message, args):
    """
    Create a note on the target user.
    """
    if not isinstance(target_arg := args.next_arg(), commands.UserMentionArg):
        # TODO: Usage
        return
    targetid = target_arg.id

    note = args.get_rest()
    if not note:
        # Request the note dynamically
        prompt = await msg.channel.send(
            "Please enter the note:"
        )
        _del_reaction = '❌'
        await prompt.add_reaction(_del_reaction)
        msg_task = asyncio.create_task(
            client.wait_for(
                'message',
                check=lambda msg_: (
                    (msg_.channel == msg.channel) and
                    (msg_.author == msg.author)
                )
            )
        )
        reaction_task = asyncio.create_task(
            client.wait_for(
                'raw_reaction_add',
                check=lambda p: (
                    (p.message_id == prompt.id
                     and str(p.emoji) == _del_reaction
                     and p.user_id == msg.author.id)
                )
            )
        )
        try:
            done, pending = await asyncio.wait(
                (msg_task, reaction_task),
                timeout=300,
                return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.TimeoutError:
            await msg.channel.send(
                "Note prompt timed out, please try again."
            )

        if msg_task in done:
            reaction_task.cancel()
            note = msg_task.result().content
        elif reaction_task in done:
            msg_task.cancel()
            await msg.channel.send(
                "Note prompt cancelled, no note was created."
            )

    if note:
        # Create the note ticket
        ticket = await create_ticket(
            type=TicketType.NOTE,
            modid=msg.author.id,
            targetid=targetid,
            created_at=dt.datetime.utcnow(),
            created_by=msg.author.id,
            stage=TicketStage.COMMENTED,
            status=TicketStatus.IN_EFFECT,
            comment=note
        )

        # Ack note creation
        await msg.channel.send(
            embed=discord.Embed(
                description="[#{}]({}): Note created!".format(ticket.id,
                                                              ticket.jump_link)
            )
        )


@commands.command("tickets")
@commands.command("ticket")
@priv.priv("mod")
async def cmd_ticket(msg: discord.Message, args):
    user = msg.author
    reply = msg.channel.send
    no_mentions = discord.AllowedMentions.none()

    S_Arg = commands.StringArg
    UM_Arg = commands.UserMentionArg

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
                await reply(
                    "Ticket #{} has been delivered to your DMs.".format(
                        mod.current_ticket.id
                    )
                )
    elif cmd == "queue":
        """
        Usage: ticket queue [modmention]
        Show the specified moderator's (or your own) ticket queue.
        """
        modarg = args.next_arg()
        if modarg is None or isinstance(modarg, UM_Arg):
            modid = modarg.id if modarg is not None else user.id
            embeds = None
            if modid in _ticketmods:
                mod = _ticketmods[modid]
                tickets = mod.queue

                embeds = summarise_tickets(
                    *tickets,
                    title='Queue for {}'.format(modid),
                    fmt=(
                        "[#{id}]({jump_link}): "
                        "({status}) **{type}** for {targetid!m}>"
                    )
                )

            if embeds:
                await pager(
                    msg.channel,
                    [Page(embed=embed) for embed in embeds]
                )
            else:
                await reply(
                    util.discord.format("{!m} has an empty queue!", modid),
                    allowed_mentions=no_mentions
                )
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
            await ticket.mod.ticket_removed(
                ticket,
                "Ticket #{} has been claimed by {}.".format(ticket.id,
                                                            msg.author.mention)
            )
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
                await reply(
                    util.discord.format(
                        "Ticket #{} is already assigned to {!m}",
                        ticket.id,
                        mod_arg.id
                    ),
                    allowed_mentions=no_mentions
                )
            else:
                old_mod = ticket.mod
                new_mod = get_or_create_mod(mod_arg.id)
                with ticket.batch_update():
                    ticket.modid = new_mod.modid
                    if ticket.stage != TicketStage.COMMENTED:
                        ticket.delivered_id = None
                        ticket.stage = TicketStage.NEW
                await old_mod.ticket_removed(
                    ticket,
                    reason=util.discord.format(
                        "Ticket {}# has been claimed by {!m}!",
                        ticket.id,
                        new_mod.modid
                    )
                )
                await ticket.publish()
    elif cmd == "comment":
        """
        Set or reset the duration and comment for a ticket.
        """
        # TODO: This requires splitting TicketMod.process_message a bit
        pass
    elif cmd == "append":
        """
        Append to the ticket reason.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        elif not (text := args.get_rest()):
            # TODO: Usage
            pass
        elif len(ticket.comment) + len(text) > 2000:
            await reply("Cannot append, exceeds maximum comment length!")
        else:
            with ticket.batch_update():
                ticket.comment = ticket.comment + '\n' + text
                ticket.modified_by = msg.author.id
            await ticket.publish()
            await reply(
                embed=discord.Embed(
                    description="[#{}]({}): Ticket updated.".format(
                        ticket.id,
                        ticket.jump_link
                    )
                )
            )
    elif cmd == "revert":
        """
        Manually revert a ticket.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        elif not ticket.can_revert:
            await reply(
                "This ticket type ({}) cannot be reverted!".format(ticket.title)
            )
        elif not ticket.active:
            await reply(
                embed=discord.Embed(
                    description=(
                        "[#{}]({}): Cannot be reverted as "
                        "it is no longer active!".format(ticket.id,
                                                         ticket.jump_link)
                    )
                )
            )
        else:
            await ticket.manual_revert(msg.author.id)
            await reply(
                embed=discord.Embed(
                    description="[#{}]({}): Ticket reverted.".format(
                        ticket.id,
                        ticket.jump_link
                    )
                )
            )
    elif cmd == "hide":
        """
        Hide (and revert) a ticket.
        """
        if (ticket := resolve_ticket(msg, args)) is None:
            await reply("No ticket referenced or ticket could not be found.")
        elif ticket.hidden:
            await reply(
                embed=discord.Embed(
                    description="#{}: Is already hidden!".format(ticket.id)
                )
            )
        else:
            reason = args.get_rest() or None
            await ticket.hide(msg.author.id, reason=reason)
            await reply(
                embed=discord.Embed(
                    description="#{}: Ticket hidden.".format(ticket.id)
                )
            )
    elif cmd == "show":
        """
        Show ticket(s) by ticketid or userid
        """
        arg = args.next_arg()
        if isinstance(arg, UM_Arg):
            # Collect tickets for the mentioned user
            userid = arg.id

            tickets = fetch_tickets_where(targetid=userid)
            shown, hidden = reduce(
                lambda p, t: p[t.hidden].append(t) or p,
                tickets,
                ([], [])
            )

            embeds = summarise_tickets(
                *shown,
                title='Tickets for {}'.format(userid),
                fmt="[#{id}]({jump_link}): ({status}) **{type}** by {modid!m}"
            )
            hidden_field = ', '.join(
                '#{}'.format(ticket.id) for ticket in hidden
            )

            if hidden_field:
                embeds = embeds or (
                    discord.Embed(title='Tickets for {}'.format(userid)),
                )
                embeds = (
                    embed.add_field(name="Hidden", value=hidden_field)
                    for embed in embeds
                )

            if embeds:
                await pager(
                    msg.channel,
                    [Page(embed=embed) for embed in embeds]
                )
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
            tickets = fetch_tickets_where(
                status=TicketStatus.HIDDEN,
                targetid=userid
            )
            embeds = summarise_tickets(
                *tickets,
                title='Hidden tickets for {}'.format(userid),
                fmt="#{id}: **{type}** by {modid!m}"
            )

            if embeds:
                await pager(
                    msg.channel,
                    [Page(embed=embed) for embed in embeds]
                )
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

util.discord.event("voice_state_update")(read_audit_log)
util.discord.event("member_ban")(read_audit_log)
util.discord.event("member_kick")(read_audit_log)


@util.discord.event("member_update")
async def process_member_update(before, after):
    if before.roles != after.roles:
        await read_audit_log()


@util.discord.event("message")
async def moderator_message(message):
    if message.channel.type == discord.ChannelType.private:
        if message.author.id in _ticketmods:
            await _ticketmods[message.author.id].process_message(message)


# Initial loading
async def init_setup():
    # Wait until the caches have been populated
    await client.wait_until_ready()

    if not conf.guild or not client.get_guild(int(conf.guild)):
        """
        No guild, nothing we can do. Don't proceed with setup.
        """
        logger.error(
            "Guild not configured, "
            "or can't find the configured guild! Aborting setup."
        )
        return
    # Reload the TicketMods
    await reload_mods()
    # Reload the expiring tickets
    init_ticket_expiry()
    # Trigger a read of the audit log, catch up on anything we may have missed
    await read_audit_log()


# Schedule the init task
asyncio.get_event_loop().create_task(init_setup())
