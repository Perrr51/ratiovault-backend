-- Migration: add auto_resolved column to ticker_enrichment
-- FOUNDER APPLIES TO STAGING FIRST, then prod after smoke:
--   supabase db push --project-ref pgyvsnvlaxnykyoreqlp  (staging)
--   supabase db push --project-ref fcizmgdnovwllsqqzasm   (prod, after staging smoke)
--
-- Additive-only change: nullable column, no default, no FK, no new indexes.
-- Existing RLS policies (auth.uid()=user_id) and PK (user_id, ticker) are untouched.

ALTER TABLE public.ticker_enrichment
  ADD COLUMN IF NOT EXISTS auto_resolved BOOLEAN NULL;

COMMENT ON COLUMN public.ticker_enrichment.auto_resolved IS
  'true = ISIN resolved by the auto-resolve endpoint (auditable, user can override). '
  'false or NULL = manual entry or yfinance-native ISIN.';
