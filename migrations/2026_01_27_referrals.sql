-- Referral system migration (PostgreSQL).
-- SQLite: recreate the database or apply equivalent ALTER TABLE statements manually.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(12);
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by INTEGER REFERENCES users(id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_ip VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS device_fingerprint VARCHAR;

UPDATE users
SET referral_code = upper(substr(encode(gen_random_bytes(6), 'hex'), 1, 10))
WHERE referral_code IS NULL;

ALTER TABLE users ALTER COLUMN referral_code SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_referral_code ON users (referral_code);

CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    referrer_id INTEGER NOT NULL REFERENCES users(id),
    referred_id INTEGER NOT NULL REFERENCES users(id),
    status VARCHAR NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ NULL,
    metadata JSONB NULL,
    CONSTRAINT uq_referrals_referred_id UNIQUE (referred_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer_id ON referrals (referrer_id);
CREATE INDEX IF NOT EXISTS idx_referrals_referred_id ON referrals (referred_id);
