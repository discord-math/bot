CREATE TABLE roles_review.roles (
    id BIGSERIAL NOT NULL,
    review_channel_id BIGINT NOT NULL,
    upvote_limit INTEGER NOT NULL,
    downvote_limit INTEGER NOT NULL,
    pending_role_id BIGINT,
    denied_role_id BIGINT,
    prompt TEXT[] NOT NULL,
    invitation TEXT NOT NULL,
    PRIMARY KEY (id)
);
