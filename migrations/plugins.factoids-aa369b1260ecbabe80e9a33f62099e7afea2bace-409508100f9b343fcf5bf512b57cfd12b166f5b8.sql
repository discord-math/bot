CREATE TABLE factoids.config (
    id BIGINT GENERATED ALWAYS AS (0) STORED NOT NULL,
    prefix TEXT,
    PRIMARY KEY (id)
)
