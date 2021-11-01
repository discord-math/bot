ALTER TABLE saved_files DROP COLUMN content;
ALTER TABLE saved_files ADD COLUMN local_filename TEXT;
