ALTER TABLE saved_messages ALTER COLUMN content TYPE BYTEA USING content::BYTEA;
