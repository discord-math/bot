ALTER TABLE saved_messages ALTER COLUMN content TYPE BYTEA USING CONVERT_TO(content, 'utf8');
