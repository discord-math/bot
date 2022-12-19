CREATE SCHEMA log;
ALTER TABLE saved_messages SET SCHEMA log;
ALTER TABLE log.saved_messages RENAME TO messages;
ALTER TABLE saved_files SET SCHEMA log;
ALTER TABLE log.saved_files RENAME TO files;

CREATE TABLE log.users
	( id BIGINT NOT NULL
	, set_at TIMESTAMP NOT NULL
	, username TEXT NOT NULL
	, discrim CHAR(4) NOT NULL
	, unset_at TIMESTAMP
	, PRIMARY KEY (id, set_at)
	);
CREATE TABLE log.nicks
	( id BIGINT NOT NULL
	, set_at TIMESTAMP NOT NULL
	, nick TEXT
	, unset_at TIMESTAMP
	, PRIMARY KEY (id, set_at)
	);
