CREATE SCHEMA modmail;
ALTER TABLE modmails SET SCHEMA modmail;
ALTER TABLE modmail.modmails RENAME TO messages;
CREATE TABLE modmail.threads
    ( user_id BIGINT NOT NULL
    , thread_first_message_id BIGINT NOT NULL
    , last_used TIMESTAMP NOT NULL
    );
