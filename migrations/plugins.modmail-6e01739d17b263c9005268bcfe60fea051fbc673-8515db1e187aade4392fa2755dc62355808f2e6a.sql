CREATE TABLE modmail.guilds (
    guild_id BIGSERIAL NOT NULL PRIMARY KEY,
    token TEXT NOT NULL,
    channel_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    thread_expiry INTERVAL NOT NULL
);

WITH conf AS (SELECT key, value FROM kv WHERE namespace = 'plugins.modmail')
    INSERT INTO modmail.guilds
    SELECT
        (guild.value::JSON #>> '{}')::BIGINT AS guild_id,
        token.value::JSON #>> '{}' AS token,
        (channel.value::JSON #>> '{}')::BIGINT AS channel_id,
        (role.value::JSON #>> '{}')::BIGINT AS role_id,
        (thread_expiry.value::JSON #>> '{}')::BIGINT
            * INTERVAL '1 second' AS thread_expiry
    FROM
        conf AS guild,
        conf AS token,
        conf AS channel,
        conf AS role,
        conf AS thread_expiry
    WHERE guild.key = ARRAY['guild']
        AND token.key = ARRAY['token']
        AND channel.key = ARRAY['channel']
        AND role.key = ARRAY['role']
        AND thread_expiry.key = ARRAY['thread_expiry'];

DELETE FROM kv WHERE namespace = 'plugins.modmail';
