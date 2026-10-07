-- Migration: add oco_placement_attempts column to track OCO retry count
-- This prevents infinite Telegram spam when OCO placement keeps failing.
-- After MAX_OCO_RETRIES (3), the bot stops retrying and sends final escalation alert.
-- To reset and allow retries again: UPDATE trades_tokocrypto SET oco_state = NULL, oco_placement_attempts = 0 WHERE entry_order_id = '<oid>';

ALTER TABLE "Toko_Crypto_Spot"
ADD COLUMN IF NOT EXISTS oco_placement_attempts INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN "Toko_Crypto_Spot".oco_placement_attempts IS 'Number of OCO placement attempts. Bot stops retrying at 3.';
