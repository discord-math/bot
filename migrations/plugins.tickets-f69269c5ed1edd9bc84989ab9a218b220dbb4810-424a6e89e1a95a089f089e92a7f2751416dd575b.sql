ALTER TABLE tickets.tickets ADD COLUMN approved BOOLEAN NOT NULL DEFAULT 'true';
ALTER TABLE tickets.tickets ALTER COLUMN approved DROP DEFAULT;
ALTER TABLE tickets.history ADD COLUMN approved BOOLEAN;

DROP TRIGGER log_update ON tickets.tickets;
DROP FUNCTION tickets.log_ticket_update;

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
                ( version, last_modified_at, id, type, stage, status, modid, targetid, roleid, auditid
                , duration, comment, approved, list_msgid, delivered_id, created_at, modified_by )
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
                    , OLD.approved
                    , OLD.list_msgid
                    , OLD.delivered_id
                    , OLD.created_at
                    , OLD.modified_by
                    );
            last_version = 0;
        END IF;
        INSERT INTO tickets.history
            ( version, last_modified_at, id, type, stage, status, modid, targetid, roleid, auditid
            , duration, comment, approved, list_msgid, delivered_id, created_at, modified_by )
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
                , NULLIF(NEW.approved, OLD.approved)
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
