-- Run this ONCE in the D1 Console for the Flux database already created.
-- It adds the two support tables and two columns required by the Worker.
-- Your existing users, memories, reminders, product_watches and flight_watches stay untouched.

CREATE TABLE IF NOT EXISTS chat_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id_id
  ON chat_history(chat_id, id DESC);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings(key, value) VALUES('startup_auto', '0');

ALTER TABLE startup_items ADD COLUMN published_at TEXT;
ALTER TABLE startup_items ADD COLUMN first_seen_at TEXT;

UPDATE startup_items
SET first_seen_at = COALESCE(discovered_at, CURRENT_TIMESTAMP)
WHERE first_seen_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_startup_items_first_seen
  ON startup_items(first_seen_at DESC);
