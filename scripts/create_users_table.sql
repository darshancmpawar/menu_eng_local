-- =============================================================================
-- Users table for Ikigai Masala authentication
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor > New query)
-- =============================================================================

CREATE TABLE IF NOT EXISTS users (
    email          TEXT PRIMARY KEY,
    profile_name   TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('super_admin', 'admin', 'user')),
    created_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- Enable RLS with permissive policy (single-tenant app)
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Allow all on users') THEN
    CREATE POLICY "Allow all on users" ON users FOR ALL USING (true) WITH CHECK (true);
  END IF;
END
$$;

-- No seed user here — run scripts/seed_admin.py with ADMIN_EMAIL and
-- ADMIN_PASSWORD env vars set to create the first super_admin. Keeping
-- credentials out of source avoids shipping a known password / hash.
