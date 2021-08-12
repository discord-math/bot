BEGIN;

ALTER TYPE tickets.TicketStatus RENAME TO OldTicketStatus;

CREATE TYPE tickets.TicketStatus AS ENUM
	( 'IN_EFFECT'
	, 'EXPIRED'
	, 'EXPIRE_FAILED'
	, 'REVERTED'
	, 'HIDDEN'
	);

ALTER TABLE tickets.tickets ALTER COLUMN status TYPE tickets.TicketStatus USING
	CASE
		WHEN status = 'NEW' THEN 'IN_EFFECT'
		ELSE status::TEXT::tickets.TicketStatus
	END;
ALTER TABLE tickets.history ALTER COLUMN status TYPE tickets.TicketStatus USING
	CASE
		WHEN status = 'NEW' THEN 'IN_EFFECT'
		ELSE status::TEXT::tickets.TicketStatus
	END;

DROP TYPE tickets.OldTicketStatus;

ALTER TABLE tickets.tickets ADD FOREIGN KEY (modid) REFERENCES tickets.mods (modid);

ALTER TABLE tickets.mods ADD COLUMN scheduled_delivery TIMESTAMP;

ALTER TABLE tickets.mods DROP COLUMN last_prompt_msgid;

CREATE INDEX tickets_mod_queue ON tickets.tickets USING BTREE (modid, id) WHERE stage <> 'COMMENTED';

CREATE OR REPLACE FUNCTION tickets.log_ticket_update()
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

COMMIT;
