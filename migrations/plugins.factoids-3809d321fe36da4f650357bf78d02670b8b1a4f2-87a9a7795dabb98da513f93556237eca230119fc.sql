ALTER TABLE factoids.factoids DROP CONSTRAINT factoids_pkey;
ALTER TABLE factoids.factoids ADD COLUMN id SERIAL PRIMARY KEY;

CREATE TABLE factoids.aliases
	( name TEXT NOT NULL PRIMARY KEY
	, id INTEGER NOT NULL REFERENCES factoids.factoids(id)
	, author_id BIGINT NOT NULL
	, created_at TIMESTAMP NOT NULL
	, uses BIGINT NOT NULL
	, used_at TIMESTAMP
	);

INSERT INTO factoids.aliases (name, id, author_id, created_at, uses, used_at)
	SELECT name, id, author_id, created_at, uses, used_at FROM factoids.factoids;

ALTER TABLE factoids.factoids DROP COLUMN name;
