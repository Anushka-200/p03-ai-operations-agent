-- The Telegram bot shows each PENDING approval to the owner exactly once. NULL = not shown yet.
-- A column on the row (not an in-memory set) so a bot restart neither forgets nor repeats.
ALTER TABLE approvals ADD COLUMN notified_at timestamptz;
