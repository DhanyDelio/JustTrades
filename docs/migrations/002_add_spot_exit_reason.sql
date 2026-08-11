-- Persist the source of every spot position exit for clean trade datasets.
-- Allowed application values:
--   SL_HIT
--   TP_HIT
--   UNPROTECTED_SL_BREACH
--   UNPROTECTED_TP_BREACH

ALTER TABLE trades_spot
  ADD COLUMN IF NOT EXISTS exit_reason TEXT DEFAULT NULL;

COMMENT ON COLUMN trades_spot.exit_reason IS
  'Exit classification: SL_HIT, TP_HIT, UNPROTECTED_SL_BREACH, or UNPROTECTED_TP_BREACH';
