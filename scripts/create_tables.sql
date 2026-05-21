-- =============================================================================
-- Supabase schema for Ikigai Masala client configuration
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor > New query)
-- =============================================================================

-- 1. Menu categories — the slot presets (menu_cat_1 … menu_cat_N)
CREATE TABLE IF NOT EXISTS menu_categories (
    name  TEXT PRIMARY KEY,
    slots TEXT[] NOT NULL
);

-- 2. Clients — each client references a menu category.
-- ``version`` is an optimistic-concurrency counter: GET /client-config
-- returns the current value; PUT must send the same value back and
-- fails with 409 Conflict if another writer bumped it in the meantime.
CREATE TABLE IF NOT EXISTS clients (
    name           TEXT PRIMARY KEY,
    menu_category  TEXT NOT NULL REFERENCES menu_categories(name),
    version        INT  NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ DEFAULT now()
);
-- Migration for deployments that created the table before the column
-- existed. No-op on fresh installs since CREATE TABLE above includes it.
ALTER TABLE clients ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;

-- 3. Slot count overrides — e.g. Rippling gets veg_dry x2
CREATE TABLE IF NOT EXISTS slot_count_overrides (
    client_name TEXT NOT NULL REFERENCES clients(name) ON DELETE CASCADE,
    slot        TEXT NOT NULL,
    count       INT  NOT NULL DEFAULT 1 CHECK (count >= 0),
    PRIMARY KEY (client_name, slot)
);

-- 4. Theme overrides — per-client day-to-theme mapping
CREATE TABLE IF NOT EXISTS theme_overrides (
    client_name TEXT NOT NULL REFERENCES clients(name) ON DELETE CASCADE,
    day         TEXT NOT NULL CHECK (day IN ('monday','tuesday','wednesday','thursday','friday')),
    theme       TEXT NOT NULL CHECK (theme IN ('mix','chinese','biryani','south','north')),
    PRIMARY KEY (client_name, day)
);

-- 5. App-level settings (core_min_one_slots, constant_slots, fallback, etc.)
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT  PRIMARY KEY,
    value JSONB NOT NULL
);

-- Note: menu_history and week_signatures are defined in
-- create_history_tables.sql (with FK + UNIQUE INDEX safety nets).
-- They were briefly duplicated here in an earlier revision; keeping
-- them in a single file removes the order-of-application footgun.

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_slot_overrides_client ON slot_count_overrides(client_name);
CREATE INDEX IF NOT EXISTS idx_theme_overrides_client ON theme_overrides(client_name);

-- =============================================================================
-- Enable Row Level Security (keep tables accessible via service/anon key)
-- =============================================================================
-- RLS for menu_history / week_signatures lives in create_history_tables.sql
-- so this script only needs to be run before that one (or in any order
-- once both have been applied).
ALTER TABLE menu_categories     ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients             ENABLE ROW LEVEL SECURITY;
ALTER TABLE slot_count_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE theme_overrides     ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_settings        ENABLE ROW LEVEL SECURITY;

-- Allow full access via the anon key (single-tenant app)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Allow all on menu_categories') THEN
    CREATE POLICY "Allow all on menu_categories"     ON menu_categories     FOR ALL USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Allow all on clients') THEN
    CREATE POLICY "Allow all on clients"             ON clients             FOR ALL USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Allow all on slot_count_overrides') THEN
    CREATE POLICY "Allow all on slot_count_overrides" ON slot_count_overrides FOR ALL USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Allow all on theme_overrides') THEN
    CREATE POLICY "Allow all on theme_overrides"     ON theme_overrides     FOR ALL USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Allow all on app_settings') THEN
    CREATE POLICY "Allow all on app_settings"        ON app_settings        FOR ALL USING (true) WITH CHECK (true);
  END IF;
END
$$;
