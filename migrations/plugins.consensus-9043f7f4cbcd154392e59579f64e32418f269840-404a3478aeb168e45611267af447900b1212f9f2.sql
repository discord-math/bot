CREATE TYPE consensus.polltype AS ENUM ('COUNTED', 'CHOICE', 'WITH_COMMENTS', 'WITH_CONCERNS');

ALTER TABLE consensus.polls ADD COLUMN poll consensus.polltype;
UPDATE consensus.polls SET poll = 'WITH_CONCERNS';
ALTER TABLE consensus.polls ALTER COLUMN poll SET NOT NULL;

ALTER TABLE consensus.polls ADD COLUMN options TEXT[];
UPDATE consensus.polls SET options = ARRAY[E'\u2705', E'\U0001F518', E'\u274C'];
ALTER TABLE consensus.polls ALTER COLUMN options SET NOT NULL;

ALTER TABLE consensus.votes ADD COLUMN choice_index BIGINT;
UPDATE consensus.votes SET choice_index = CASE
    WHEN vote = 'UPVOTE' THEN 0
    WHEN vote = 'NEUTRAL' THEN 1
    WHEN vote = 'DOWNVOTE' THEN 2
END;
ALTER TABLE consensus.votes ALTER COLUMN choice_index SET NOT NULL;

ALTER TABLE consensus.votes DROP COLUMN vote;
DROP TYPE consensus.votetype;
