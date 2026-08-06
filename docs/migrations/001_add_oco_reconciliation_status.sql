-- Migration: add oco_reconciliation_status column to trades_spot
-- Run once in Supabase SQL Editor before the column is used.
--
-- Possible values:
--   NULL                    — normal state, OCO verified active
--   'UNPROTECTED'           — OCO not found on exchange (-2018/-2013)
--   'UNPROTECTED_SL_BREACH' — OCO missing AND price already below SL
--   'RECONCILIATION_REQUIRED' — transient API error, status unknown

ALTER TABLE trades_spot
  ADD COLUMN IF NOT EXISTS oco_reconciliation_status TEXT DEFAULT NULL;

COMMENT ON COLUMN trades_spot.oco_reconciliation_status IS
  'OCO protection reconciliation state: NULL=OK, UNPROTECTED, UNPROTECTED_SL_BREACH, RECONCILIATION_REQUIRED';
