-- Try-before-signup migration (PostgreSQL).
-- SQLite: recreate the database or apply equivalent ALTER TABLE statements manually.

ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS free_used INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google_id ON users (google_id);

ALTER TABLE essays ADD COLUMN IF NOT EXISTS anon_id VARCHAR;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'essays' AND column_name = 'user_id'
    ) THEN
        ALTER TABLE essays ALTER COLUMN user_id DROP NOT NULL;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_essays_anon_id ON essays (anon_id);

CREATE TABLE IF NOT EXISTS anonymous_sessions (
    id SERIAL PRIMARY KEY,
    anon_id VARCHAR NOT NULL UNIQUE,
    free_used INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NULL,
    last_ip VARCHAR NULL,
    device_id VARCHAR NULL,
    linked_user_id INTEGER NULL REFERENCES users(id),
    linked_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_anonymous_sessions_anon_id ON anonymous_sessions (anon_id);
CREATE INDEX IF NOT EXISTS idx_anonymous_sessions_linked_user_id ON anonymous_sessions (linked_user_id);
