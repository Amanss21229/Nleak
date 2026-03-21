CREATE TABLE IF NOT EXISTS bot_users (
    user_id BIGINT PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    mobile TEXT DEFAULT '',
    alt_mobile TEXT DEFAULT '',
    gmail TEXT DEFAULT '',
    alt_gmail TEXT DEFAULT '',
    referred_by BIGINT,
    is_verified BOOLEAN DEFAULT FALSE,
    verified_at TIMESTAMPTZ,
    data_submitted BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    referrer_id BIGINT NOT NULL REFERENCES bot_users(user_id),
    referred_id BIGINT NOT NULL REFERENCES bot_users(user_id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (referrer_id, referred_id)
);

CREATE TABLE IF NOT EXISTS message_map (
    group_msg_id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES bot_users(user_id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
