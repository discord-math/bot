import re
import itertools
import datetime
import asyncio
import logging
import contextlib
from functools import reduce
from collections import namedtuple
from enum import Enum, IntEnum
from typing import List


import discord

import discord_client
import util.discord
from util.db.initialization import init_for
from util import db

import plugins.commands as commands
import plugins.privileges as priv


logger = logging.getLogger(__name__)

# ---------- Constants ----------
ticket_schema = """\
CREATE SCHEMA tickets;


CREATE TABLE tickets.tickets (
        id 				SERIAL 	PRIMARY KEY,	-- Ticket id
        type 			INT		NOT NULL,		-- Ticket type, referencing hard-coded enum
        stage 			INT		NOT NULL,		-- Ticket stage, referencing hard-coded enum
        status 			INT 	NOT NULL,		-- Ticket status, referencing hard-coded enum
        modid 			BIGINT	NOT NULL,		-- ID of acting moderator
        targetid 		BIGINT	NOT NULL,		-- ID of target user
        roleid 			BIGINT,					-- ID of role added (if applicable)
        auditid 		BIGINT,					-- ID of audit entry (if applicable)
        duration 		INT,					-- Ticket duration in seconds
        comment 		TEXT,					-- Ticket comment/reason
        list_msgid 		BIGINT,					-- ID of ticket message in ticket list
        delivered_id 	BIGINT,					-- ID of ticket message sent to moderator
        created_at 		TIMESTAMP,				-- Timestamp of ticket creation (based on original action)
        modified_by		BIGINT					-- ID of last user to edit the ticket
);

CREATE TABLE tickets.mods (
        modid BIGINT PRIMARY KEY,
        last_read_msgid BIGINT,
        last_prompt_msgid BIGINT
);

CREATE TABLE tickets.tracked_roles(
        roleid BIGINT PRIMARY KEY
);

CREATE TABLE tickets.history (
        version INT,
        last_modified_at TIMESTAMP,
        id INT,
        type INT,
        stage INT,
        status INT,
        modid BIGINT,
        targetid BIGINT,
        roleid BIGINT,
        auditid BIGINT,
        duration INT,
        comment TEXT,
        list_msgid BIGINT,
        delivered_id BIGINT,
        created_at TIMESTAMP,
        modified_by BIGINT,
        PRIMARY KEY (id, version),
        FOREIGN KEY (id) REFERENCES tickets.tickets ON UPDATE CASCADE
);

CREATE FUNCTION tickets.log_ticket_update() RETURNS TRIGGER AS $log_ticket_update$
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
                SELECT version INTO last_version FROM tickets.history WHERE id = OLD.id ORDER BY version DESC LIMIT 1;
                IF NOT FOUND THEN
                        INSERT into tickets.history VALUES (0, OLD.created_at, OLD.*), (1, now(), modified.*);
                ELSE
                        INSERT into tickets.history VALUES (coalesce(last_version + 1, 1), now(), modified.*);
                END IF;
                RETURN NULL;
        END
$log_ticket_update$ LANGUAGE plpgsql;

CREATE TRIGGER log_update
        AFTER UPDATE ON tickets.tickets
        FOR EACH ROW
        WHEN (OLD.* IS DISTINCT FROM NEW.*)
        EXECUTE PROCEDURE tickets.log_ticket_update();
"""

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
    """.replace(' ', '').replace('\n', '')
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
conf = db.kv.Config(__name__)  # General plugin configuration

conf.guild: int  # ID of the guild the ticket system is managing
conf.tracked_roles: List[int]  # List of roleids of tracked roles
conf.last_audit_id: int  # ID of last audit event processed
conf.ticket_list: int  # Channel id of the ticket list in the guild


# ----------- Data -----------
@init_for(__name__)
def init():
    return ticket_schema


class fieldConstants(Enum):
    """
    A collection of database field constants to use for selection conditions.
    """
    NULL = "IS NULL"
    NOTNULL = "IS NOT NULL"


class _rowInterface:
    __slots__ = ('row', '_pending')

    _conn = db.connection()

    _table = None
    _id_col = None
    _columns = {}

    def __init__(self, row, *args, **kwargs):
        self.row = row
        self._pending = None

    def __repr__(self):
        return "{}({})".format(
            self.__class__.__name__,
            ', '.join("{}={!r}".format(field, getattr(self, field)) for field in self._columns)
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
            raise ValueError("Nested batch updates for {}!".format(self.__class__.__name__))

        self._pending = {}
        try:
            yield self._pending
        finally:
            self.update(**self._pending)
            self._pending = None

    def _refresh(self):
        rows = self._select_where(**{self._columns[self._id_col]: self.row[self._id_col]})
        if not rows:
            raise ValueError("Refreshing a {} which no longer exists!".format(type(self).__name__))
        self.row = rows[0]

    def update(self, **values):
        rows = self._update_where(values, **{self._columns[self._id_col]: self.row[self._id_col]})
        if not rows:
            raise ValueError("Updating a {} which no longer exists!".format(type(self).__name__))
        self.row = rows[0]

    @staticmethod
    def format_conditions(conditions):
        if not conditions:
            return ("", tuple())

        values = []
        conditional_strings = []
        for key, item in conditions.items():
            if isinstance(item, (list, tuple)):
                conditional_strings.append("{} IN ({})".format(key, ", ".join(['%s'] * len(item))))
                values.extend(item)
            elif isinstance(item, fieldConstants):
                conditional_strings.append("{} {}".format(key, item.value))
            else:
                conditional_strings.append("{}='%s'".format(key))
                values.append(item)

        return (' AND '.join(conditional_strings), values)

    @classmethod
    def _select_where(cls, _extra=None, **conditions):
        with cls._conn as conn:
            with conn.cursor() as cursor:
                cond_str, cond_values = cls.format_conditions(conditions)

                cursor.execute(
                    "SELECT * FROM {} {} {} {}".format(
                        cls._table, 'WHERE' if conditions else '', cond_str, _extra or ''
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
                    "INSERT INTO {} ({}) VALUES ({}) RETURNING *".format(cls._table, columns, value_str),
                    values
                )
                return cursor.fetchone()

    @classmethod
    def _update_where(cls, values, **conditions):
        with cls._conn as conn:
            with conn.cursor() as cursor:
                cond_str, cond_values = cls.format_conditions(conditions)
                value_str = ', '.join('{}=%s'.format(key) for key in values.keys())
                values = tuple(values.values())

                cursor.execute(
                    "UPDATE {} SET {} WHERE {} RETURNING *".format(cls._table, value_str, cond_str),
                    (*values, *cond_values)
                )
                return cursor.fetchall()


# ----------- Tickets -----------

class _FieldEnum(IntEnum):
    """
    Truthy integer enum conforming to the ISQLQuote protocol for processing by psycopg.
    """
    def __bool__(self):
        return True

    def __conform__(self, proto):
        return self

    def getquoted(self):
        return str(self.value).encode('utf8')


class TicketType(_FieldEnum):
    """
    The possible types of tickets, represented as the corresponding moderation action.
    """
    NOTE = 1
    KICK = 2
    BAN = 3
    VC_MUTE = 4
    VC_DEAFEN = 5
    ADD_ROLE = 6


class TicketStatus(_FieldEnum):
    """
    Possible values for the current status of a ticket.
    """
    def __new__(cls, value, desc):
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj.desc = desc
        return obj

    def __repr__(self):
        return '<%s.%s>' % (self.__class__.__name__, self.name)

    NEW = 1, 'New'  # New, uncommented and active ticket
    IN_EFFECT = 2, 'In effect'  # Commented and active ticket
    EXPIRED = 3, 'Expired'  # Ticket's duration has expired, may be (un)commented
    REVERTED = 4, 'Manually reverted'  # Ticket has been manually reverted, may be (un)commented
    HIDDEN = 5, 'Hidden'  # Ticket is inactive and has been hidden, may be (un)commented


class TicketStage(_FieldEnum):
    """
    The possible stages of delivery of a ticket to the responsible moderator.
    """
    NEW = 1
    DELIVERED = 2
    COMMENTED = 3


class Ticket(_rowInterface):
    __slots__ = tuple()

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

    trigger_action = None  # AuditLogAction triggering automatic ticket creation
    revert_trigger_action = None  # AuditLogAction triggering automatic ticket reversal

    @property
    def embed(self) -> discord.Embed:
        """
        The discord embed describing this ticket.
        """
        embed = discord.Embed(
            title=self.title,
            description=self.comment or "No comment",
            timestamp=self.created_at
        )
        embed.set_author(name="Ticket #{} ({})".format(self.id, TicketStatus(self.status).desc))
        embed.set_footer(text="Moderator: {}".format(self.mod.user or self.modid))
        embed.add_field(
            name="Target",
            value="<@{0}>\n({0})".format(self.targetid)
        )
        if self.roleid:
            embed.add_field(
                name="Role",
                value="{}\n({})".format(role.name, role.id) if (role := self.role) else str(self.roleid)
            )
        if self.duration:
            embed.add_field(name="Duration", value=str(datetime.timedelta(seconds=self.duration)))
        return embed

    @property
    def history(self):
        """
        The modification history of this ticket as a list of TicketHistory rows.
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
    def expiry(self) -> datetime.datetime:
        """
        Expiry timestamp for this ticket, if applicable.
        """
        if self.can_revert and self.duration is not None:
            return self.created_at + datetime.timedelta(seconds=self.duration)

    @property
    def mod(self):
        """
        TicketMod associated to this ticket.
        """
        return get_or_create_mod(self.modid)

    @property
    def target(self) -> discord.Member:
        return discord_client.client.get_guild(conf.guild).get_member(self.targetid)

    @property
    def role(self) -> discord.Role:
        return discord_client.client.get_guild(conf.guild).get_role(self.roleid)

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
        fmt = fmt or "[#{id}]({jump_link})(`{status:<9}`): **{type}** for <@{targetid}> by <@{modid}>."

        fmt_dict = {field: self.row[i] for i, field in enumerate(self._columns)}
        fmt_dict['status'] = TicketStatus(self.status).name
        fmt_dict['stage'] = TicketStage(self.stage).name
        fmt_dict['type'] = TicketType(self.type).name

        return fmt.format(
            ticket=self,
            title=self.title,
            jump_link=self.jump_link,
            **fmt_dict
        )

    async def publish(self):
        """
        Ticket update hook.
        Should be run whenever a ticket is created or updated.
        Manages the ticket list embed, and defers to the expiry and ticket mod update hooks.
        """
        # Reschedule or cancel ticket expiry if required
        update_expiry_for(self)

        # Post to or update the ticket list
        if conf.ticket_list:
            channel = discord_client.client.get_channel(conf.ticket_list)
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
        result = await self._revert_action(reason="Ticket #{}: Automatic expiry.".format(self.id))
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
            reason="Ticket #{}: Moderator {} requested revert.".format(self.id, actorid)
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
            reason="Ticket #{}: Moderator {} hid the ticket.".format(self.id, actorid)
        )
        if result:
            with self.batch_update():
                self.status = TicketStatus.HIDDEN
                self.modified_by = actorid
                if reason is not None:
                    self.comment = self.comment + '\n' + reason
            await self.publish()
        return result


# Decorator to register Ticket subclasses for each TicketType
_ticket_types = {}  # Map of ticket types to the associated class.
ticket_action_handlers = {}  # Map of audit actions to the associated handler methods.


def _ticket_type(cls):
    _ticket_types[cls._type] = cls
    if cls.trigger_action is not None:
        if cls.trigger_action in ticket_action_handlers:
            ticket_action_handlers[cls.trigger_action].append(cls.create_from_audit)
        else:
            ticket_action_handlers[cls.trigger_action] = [cls.create_from_audit]
    if cls.revert_trigger_action is not None:
        if cls.revert_trigger_action in ticket_action_handlers:
            ticket_action_handlers[cls.revert_trigger_action].append(cls.revert_from_audit)
        else:
            ticket_action_handlers[cls.revert_trigger_action] = [cls.revert_from_audit]


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
        self.update(status=TicketStatus.HIDDEN, modified_by=modified_by.id)
        await self.publish()

    async def expire(self, **kwargs):
        """
        Expiring notes are hidden.
        """
        self.update(status=TicketStatus.HIDDEN, modified_by=0)
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
        guild = discord_client.client.get_guild(conf.guild)
        bans = await guild.bans()
        user = next((entry.user for entry in bans if entry.user.id == self.targetid), None)
        if user is None:
            # User is already unbanned, nothing to do
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
        guild = discord_client.client.get_guild(conf.guild)
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
        guild = discord_client.client.get_guild(conf.guild)
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
                if conf.tracked_roles and role.id in conf.tracked_roles:
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
                if conf.tracked_roles and role.id in conf.tracked_roles:
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
        guild = discord_client.client.get_guild(conf.guild)
        role = guild.get_role(self.roleid)
        if role is None:
            return False
        target = guild.get_member(self.targetid)
        if target is None:
            return None
        await target.remove_roles(role)
        return True


async def _read_audit_log(*args):
    """
    Read the audit log from the last read value and process the new audit events.
    If there is no last read value, just reads the last value.
    """
    # TODO: Lock so we don't read simultaneously
    if not conf.guild or not (guild := discord_client.client.get_guild(conf.guild)):
        """
        Nothing we can do
        """
        logger.critical("Guild not configured, or can't find the configured guild! Cannot read audit log.")
        return

    logger.debug("Reading audit entries since {}".format(conf.last_auditid))
    if conf.last_auditid:
        entries = [await guild.audit_logs(limit=100, oldest_first=True).flatten()]
        # If there is more than one page of new entries, keep collecting them
        while entries[-1][0].id > conf.last_auditid:
            new_entries = await guild.audit_logs(limit=100, before=entries[0], oldest_first=True).flatten()
            if not new_entries:
                break
            entries.append(new_entries)
        entries = filter(lambda entry: entry.id > conf.last_auditid, itertools.chain.from_iterable(reversed(entries)))
    else:
        # With no know last auditid, just read the last entry
        entries = await guild.audit_logs(limit=1).flatten()

    # Process each audit entry
    for entry in entries:
        logger.debug("Processing audit entry {}".format(entry))
        if entry.user != discord_client.client.user and entry.action in ticket_action_handlers:
            [await handler(entry) for handler in ticket_action_handlers[entry.action]]
        conf.last_auditid = entry.id


def fetch_tickets_where(**kwargs):
    """
    Fetch Tickets matching the given conditions.
    Values must be given in data-compatible form, i.e. values for Enums and datetime objects for timestamps.
    Lists of values are supported and will be converted to `IN` conditionals.
    """
    rows = Ticket._select_where(**kwargs)
    return ((_ticket_types[TicketType(row[Ticket._columns.index('type')])])(row) for row in rows)


async def create_ticket(type: TicketType, modid: int, targetid: int, created_at: datetime.datetime, created_by: int,
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
    logger.debug("Waiting for Ticket #{} to expire. (Expires at {})".format(_next_expiring[0], _next_expiring[1]))
    try:
        await asyncio.sleep(_next_expiring[1].timestamp() - datetime.datetime.utcnow().timestamp())
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
    _expiring_tickets = {ticket.id: ticket.expiry for ticket in expiring_tickets}
    # TODO: Log init
    logger.info("Loaded {} expiring tickets.".format(len(_expiring_tickets)))
    _reload_expiration()


# ----------- Ticket Mods and queue management -----------
_ticketmods = {}


class TicketMod(_rowInterface):
    __slots__ = (
        'current_ticket',
        '_prompt_task',
        '_delivery_task',
        '_current_ticket_msg'
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
        self._current_ticket_msg = None
        logger.debug("Initialised ticket mod {}. Next ticket: {}".format(self.modid, self.current_ticket))

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
        return discord_client.client.get_user(self.modid)

    async def get_ticket_message(self):
        """
        Get the current ticket delivery message in the DM, if it exists.
        """
        if self.current_ticket and self.current_ticket.delivered_id:
            if not self._current_ticket_msg or self._current_ticket_msg.id != self.current_ticket.delivered_id:
                # Update the cached message
                self._current_ticket_msg = await self.user.fetch_message(self.current_ticket.delivered_id)
            return self._current_ticket_msg

    async def load(self):
        """
        Initial TicketMod loading to be run on initial launch.
        Safe to run outside of launch.
        """
        # Process any missed messages, and schedule prompt or delivery for the current ticket as needed
        if self.current_ticket:
            logger.debug("Loading moderator {}.".format(self.modid))
            if self.current_ticket.stage == TicketStage.NEW:
                # In this case, the ticket at the top of their queue wasn't delivered.
                # We ignore missed messages since we can't be certain what ticket they refer to

                # Schedule ticket delivery
                await self.deliver()
            else:
                # The last ticket was delivered, but not yet commented
                # Replay any messages we missed, and process the first message as a comment, if it exists
                # We can't tell what the rest of the messages refer to, so we ignore them

                # Message snowflake to process from
                last_read = discord.Object(max(self.last_read_msgid or 0, self.current_ticket.delivered_id))

                # Collect the missed messages
                mod_messages = []
                if self.user:
                    messages = await self.user.history(after=last_read, limit=None).flatten()
                    mod_messages = [message for message in messages if message.author.id == self.modid]

                if mod_messages:
                    logger.debug("Missed {} messages from moderator {}.".format(len(mod_messages), self.modid))

                    # Process the first missed message
                    await self.process_message(mod_messages[0])
                    # Save the last missed message as the last one handled
                    if len(mod_messages) > 1:
                        self.last_read_msgid = mod_messages[-1].id
                else:
                    # If we didn't process a comment, schedule the reminder prompt for the current ticket
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
        if self._prompt_task and not self._prompt_task.cancelled() and not self._prompt_task.done():
            self._prompt_task.cancel()
        if self._delivery_task and not self._delivery_task.cancelled() and not self._delivery_task.done():
            self._delivery_task.cancel()

    def get_current_ticket(self) -> Ticket:
        # Get current ticket
        ticket = fetch_tickets_where(
            modid=self.modid,
            stage=[TicketStage.DELIVERED, TicketStage.NEW],
            _extra="ORDER BY stage DESC, id ASC LIMIT 1"
        )
        return next(ticket, None)

    def perm_check(self):
        """
        Check that this user has moderator permissions.
        """
        # TODO
        return True

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
        if self.last_prompt_msgid:
            # Wait until the next prompt is due
            next_prompt = discord.Object(self.last_prompt_msgid).created_at.timestamp() + self.prompt_interval
            try:
                await asyncio.sleep(next_prompt - datetime.datetime.utcnow().timestamp())
            except asyncio.CancelledError:
                return

        user = self.user
        if user is not None:
            if self.last_prompt_msgid and self.last_prompt_msgid != self.current_ticket.delivered_id:
                # Delete last prompt
                try:
                    old_prompt = await user.fetch_message(self.last_prompt_msgid)
                    await old_prompt.delete()
                except discord.HTTPException:
                    pass
            # Send new prompt
            try:
                ticket_msg = await self.get_ticket_message()
                prompt_msg = await user.send("Please comment on the above action!", reference=ticket_msg)
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
                self.current_ticket = ticket
                # If the current ticket has been updated, update the current ticket message
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
            await self.user.send(reason or "Ticket #{} was removed from your queue!".format(ticket.id))

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
            logger.debug("Delivering ticket #{} to mod {}".format(self.current_ticket.id, self.modid))
            try:
                self._current_ticket_msg = await self.user.send(
                    content="Please comment on the below ticket!",
                    embed=self.current_ticket.embed
                )
            except discord.HTTPException:
                # Reschedule
                pass
            else:
                # Set current ticket to being delivered
                self.current_ticket.update(stage=TicketStage.DELIVERED,
                                           delivered_id=self._current_ticket_msg.id)

                # Update the last prompt message
                self.last_prompt_msgid = self._current_ticket_msg.id

                # (Re-)schedule the next prompt update
                await self.schedule_prompt()

    async def process_message(self, message):
        """
        Process a non-command message from the moderator.
        If there is a current active ticket, treat it as a comment.
        Either way, update the last handled message in data.
        """
        if not priv.has_privilege('mod', message.author):
            # Don't process messages from non-moderators at all.
            return

        if not commands.conf.prefix or not message.content.startswith(commands.conf.prefix):
            content = message.content
            if ticket := self.current_ticket:
                logger.info(
                    "Processing message from moderator {} as comment to ticket #{}: {}".format(self.modid,
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
                    ticket.duration = duration if (ticket.can_revert and ticket.active) else None
                    ticket.comment = comment
                    ticket.modified_by = self.modid
                    if ticket.status == TicketStatus.NEW:
                        ticket.status = TicketStatus.IN_EFFECT

                self.last_read_msgid = message.id

                # Notify the moderator, nullify the duration if required
                if duration:
                    if not ticket.can_revert:
                        msg = ("Ticket comment set! "
                               "Provided duration ignored since this ticket type cannot expire.")
                        duration = None
                    elif not ticket.active:
                        msg = ("Ticket comment set! "
                               "Provided duration ignored since this ticket is no longer in effect.")
                        duration = None
                    else:
                        msg = "Ticket comment and duration set!"
                else:
                    msg = "Ticket comment set!"

                await self.user.send(msg)

                # Publish the ticket, which will also trigger an update of the local ticket embed
                await self.current_ticket.publish()

                # Schedule ticket expiration, if required
                if duration:
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
    [mod.unload() for mod in _ticketmods.values()]
    _ticketmods = {row[0]: TicketMod(row) for row in TicketMod._select_where()}
    [await mod.load() for mod in _ticketmods.values()]
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

def resolve_ticket(msg, ticketarg=None) -> Ticket:
    if ticketarg is not None and isinstance(ticketarg, commands.StringArg):
        maybe_id = int(ticketarg.text)
        if maybe_id < 2147483647:
            tickets = fetch_tickets_where(id=maybe_id)
        else:
            tickets = fetch_tickets_where(list_msgid=maybe_id)
        return next(tickets, None)
    elif ref := msg.reference:
        if (ref_msg := ref.resolved) and isinstance(ref_msg, discord.Message):
            if ref_msg.author == discord_client.client.user and ref_msg.embeds:
                embed = ref_msg.embeds[0]
                if embed.author.name and embed.author.name.startswith("Ticket #"):
                    ticket_id = int(embed.author.name[8:].split(' ', maxsplit=1)[0])
                    tickets = fetch_tickets_where(id=ticket_id)
                    return next(tickets, None)


def summarise_tickets(*tickets, title="Tickets", fmt=None):
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


Page = namedtuple('Page', ('content', 'embed'), defaults=(None, None))


async def pager(dest: discord.abc.Messageable, *pages):
    """
    Page a sequence of pages.
    """
    _next_reaction = '⏭️'
    _prev_reaction = '⏮️'

    # Sanity check
    if not pages:
        raise ValueError("Cannot page with no pages!")

    # Send first page
    msg = await dest.send(**pages[0]._asdict())

    if len(pages) == 1:
        return

    # Add reactions
    await msg.add_reaction(_prev_reaction)
    await msg.add_reaction(_next_reaction)

    index = 0
    while True:
        try:
            payload = await discord_client.client.wait_for(
                'raw_reaction_add', timeout=120,
                check=lambda p: (p.message_id == msg.id
                                 and str(p.emoji) in [_next_reaction, _prev_reaction]
                                 and p.user_id != msg.guild.me.id)
            )
            if str(payload.emoji) == _next_reaction:
                index += 1
            elif str(payload.emoji) == _prev_reaction:
                index -= 1
            index %= len(pages)
            await msg.edit(**pages[index]._asdict())
            try:
                await msg.remove_reaction(payload.emoji, discord.Object(payload.user_id))
            except discord.HTTPException:
                pass
        except asyncio.TimeoutError:
            break
        except asyncio.CancelledError:
            break


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

    maybe_note_arg = args.next_arg()
    note = None
    if maybe_note_arg is None:
        # Request the note dynamically
        await msg.channel.send("Please enter the note contents, or send `c` to cancel:")
        try:
            message = await discord_client.client.wait_for(
                'message',
                timeout=300,
                check=lambda msg_: (msg_.channel == msg.channel) and (msg_.author == msg.author)
            )
        except asyncio.TimeoutError:
            await msg.channel.send("Note prompt timed out, please try again.")
        if message.content.lower() == 'c':
            await msg.channel.send("Note prompt cancelled, no note was created.")
        else:
            note = message.content
    elif isinstance(maybe_note_arg, commands.StringArg):
        note = maybe_note_arg.text
    else:
        # TODO: Usage
        note = None

    if note is not None:
        # Create the note ticket
        ticket = await create_ticket(
            type=TicketType.NOTE,
            modid=msg.author.id,
            targetid=targetid,
            created_at=datetime.datetime.utcnow(),
            created_by=msg.author.id,
            stage=TicketStage.COMMENTED,
            status=TicketStatus.IN_EFFECT,
            comment=note
        )

        # Ack note creation
        await msg.channel.send(
            embed=discord.Embed(description="[#{}]({}): Note created!".format(ticket.id, ticket.jump_link))
        )


@commands.command("tickets")
@commands.command("ticket")
@priv.priv("mod")
async def cmd_ticket(msg: discord.Message, args):
    user = msg.author
    reply = msg.channel.send
    no_mentions = discord.AllowedMentions.none()

    cmd_arg = args.next_arg()
    if not isinstance(cmd_arg, commands.StringArg):
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
        if modarg is None or isinstance(modarg, commands.UserMentionArg):
            modid = modarg.id if modarg is not None else user.id
            embeds = None
            if modid in _ticketmods:
                mod = _ticketmods[modid]
                tickets = mod.queue

                embeds = summarise_tickets(
                    *tickets,
                    title='Queue for {}'.format(modid),
                    fmt="[#{id}]({jump_link}): ({status}) **{type}** for <@{targetid}>"
                )

            if embeds:
                await pager(msg.channel, *(Page(embed=embed) for embed in embeds))
            else:
                await reply(
                    "<@{}> has an empty queue!".format(modid),
                    allowed_mentions=no_mentions
                )
    elif cmd == "take":
        """
        Usage: ticket take <ticket>
        Claim a ticket (i.e. set the responsible moderator to yourself).
        `ticket` may be specified by a ticketid, list message id, list message link, or by replying to a ticket embed.
        """
        ticketarg = args.next_arg()
        ticket = resolve_ticket(msg, ticketarg)
        if not ticket:
            await reply("No ticket referenced or ticket could not be found.")
        elif ticket.modid == msg.author.id:
            await reply("This is already your ticket!")
        else:
            ticket.update(modid=msg.author.id)
            await ticket.mod.ticket_removed(
                ticket,
                "Ticket #{} has been claimed by {}.".format(ticket.id, msg.author.mention)
            )
            await ticket.publish()
            await reply("You have claimed ticket #{}.".format(ticket.id))
    elif cmd == "assign":
        """
        Usage: ticket assign <ticket> <modmention>
        Assign the specified ticket to the specified moderator.
        """
        mod_arg = None
        ticket_arg = None

        arg1 = args.next_arg()
        if arg1 is not None:
            if (arg2 := args.next_arg()) is not None:
                ticket_arg, mod_arg = arg1, arg2
            else:
                mod_arg = arg1

        if mod_arg is None or not isinstance(mod_arg, commands.UserMentionArg):
            await reply("Please provide a moderator mention!")
        elif (ticket := resolve_ticket(msg, ticket_arg)) is None:
            await reply("No ticket referenced or ticket could not be found!")
        else:
            if mod_arg.id == ticket.modid:
                await reply(
                    "Ticket #{} is already assigned to <@{}>".format(ticket.id, mod_arg.id),
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
                    reason="Ticket {}# has been claimed by <@{}>!".format(ticket.id, new_mod.modid)
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
        ticketarg = args.next_arg()
        ticket = resolve_ticket(msg, ticketarg)
        if not ticket:
            await reply("No ticket referenced or ticket could not be found.")
        # TODO remember to check for max length
        pass
    elif cmd == "revert":
        """
        Manually revert a ticket.
        """
        ticketarg = args.next_arg()
        ticket = resolve_ticket(msg, ticketarg)
        if not ticket:
            await reply("No ticket referenced or ticket could not be found.")
        elif not ticket.can_revert:
            await reply("This ticket type ({}) cannot be reverted!".format(ticket.title))
        elif not ticket.active:
            await reply(
                embed=discord.Embed(
                    description="[#{}]({}): Cannot be reverted as it is no longer active!".format(ticket.id,
                                                                                                  ticket.jump_link)
                )
            )
        else:
            await ticket.manual_revert(msg.author.id)
            await reply(
                embed=discord.Embed(
                    description="[#{}]({}): Ticket reverted.".format(ticket.id, ticket.jump_link)
                )
            )
    elif cmd == "hide":
        """
        Hide (and revert) a ticket.
        """
        ticketarg = args.next_arg()
        ticket = resolve_ticket(msg, ticketarg)
        if not ticket:
            await reply("No ticket referenced or ticket could not be found.")
        elif ticket.hidden:
            await reply(
                embed=discord.Embed(
                    description="#{}: Is already hidden!".format(ticket.id)
                )
            )
        else:
            reason = None
            if (reason_arg := args.next_arg()):
                if isinstance(reason_arg, commands.StringArg):
                    reason = reason_arg.text
                else:
                    # TODO: Usage
                    return
            await ticket.hide(msg.author.id, reason=reason)
            await reply(embed=discord.Embed(description="#{}: Ticket hidden.".format(ticket.id)))
    elif cmd == "show":
        """
        Show ticket(s) by ticketid or userid
        """
        arg = args.next_arg()
        if isinstance(arg, commands.UserMentionArg):
            # Collect tickets for the mentioned user
            userid = arg.id

            tickets = fetch_tickets_where(targetid=userid)
            shown, hidden = reduce(lambda p, t: p[t.hidden].append(t) or p, tickets, ([], []))

            embeds = summarise_tickets(
                *shown,
                title='Tickets for {}'.format(userid),
                fmt="[#{id}]({jump_link}): ({status}) **{type}** by <@{modid}>"
            )
            hidden_field = ', '.join('#{}'.format(ticket.id) for ticket in hidden)

            if hidden_field:
                embeds = embeds or (discord.Embed(title='Tickets for {}'.format(userid)), )
                embeds = (embed.add_field(name="Hidden", value=hidden_field) for embed in embeds)

            if embeds:
                await pager(msg.channel, *(Page(embed=embed) for embed in embeds))
            else:
                await reply("No tickets found for this user.")
        elif isinstance(arg, commands.StringArg) and arg.text.isdigit():
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
        if isinstance(arg, commands.UserMentionArg):
            # Collect hidden tickets for the mentioned user
            userid = arg.id
            tickets = fetch_tickets_where(status=TicketStatus.HIDDEN, targetid=userid)
            embeds = summarise_tickets(
                *tickets,
                title='Hidden tickets for {}'.format(userid),
                fmt="#{id}: **{type}** by <@{modid}>"
            )

            if embeds:
                await pager(msg.channel, *(Page(embed=embed) for embed in embeds))
            else:
                await reply("No hidden tickets found for this user.")
        elif isinstance(arg, commands.StringArg) and arg.text.isdigit():
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

util.discord.event("voice_state_update")(_read_audit_log)
util.discord.event("member_ban")(_read_audit_log)
util.discord.event("member_kick")(_read_audit_log)


@util.discord.event("member_update")
async def process_member_update(before, after):
    if before.roles != after.roles:
        await _read_audit_log()


@util.discord.event("message")
async def moderator_message(message):
    if message.channel.type == discord.ChannelType.private and message.author.id in _ticketmods:
        await _ticketmods[message.author.id].process_message(message)


# Initial loading
async def init_setup():
    # Wait until the caches have been populated
    await discord_client.client.wait_until_ready()

    if not conf.guild or not discord_client.client.get_guild(conf.guild):
        """
        No guild, nothing we can do. Don't proceed with setup.
        """
        logger.critical("Guild not configured, or can't find the configured guild! Aborting setup.")
        return
    # Reload the TicketMods
    await reload_mods()
    # Reload the expiring tickets
    init_ticket_expiry()
    # Trigger a read of the audit log, catch up on anything we may have missed
    await _read_audit_log()


# Schedule the init task
asyncio.get_event_loop().create_task(init_setup())
