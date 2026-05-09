-- Migration: forex_rate_history table
-- Change: metals-cross-currency-pricing / Slice 1
-- Apply: supabase db push --project-ref pgyvsnvlaxnykyoreqlp (staging)
--        supabase db push --project-ref fcizmgdnovwllsqqzasm (prod — after staging smoke green)
--
-- Global write-through cache for historical forex rates.
-- Rates are fetched from ECB SDW (primary) or yfinance (fallback) by the
-- backend service and persisted here to avoid duplicate upstream calls.
-- Historical rates are immutable: ON CONFLICT DO NOTHING semantics apply.

CREATE TABLE IF NOT EXISTS public.forex_rate_history (
    base_currency  CHAR(3)     NOT NULL CHECK (base_currency  = upper(base_currency)  AND length(base_currency)  = 3),
    quote_currency CHAR(3)     NOT NULL CHECK (quote_currency = upper(quote_currency) AND length(quote_currency) = 3),
    rate_date      DATE        NOT NULL,
    rate           NUMERIC(20, 10) NOT NULL CHECK (rate > 0),
    source         VARCHAR(20) NOT NULL CHECK (source IN ('ecb', 'yfinance', 'manual')),
    actual_date    DATE        NOT NULL,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (base_currency, quote_currency, rate_date)
);

-- Comments
COMMENT ON TABLE public.forex_rate_history IS
    'Global write-through cache for historical forex rates. Read: all authenticated. Write: service_role only. Immutable once written.';

COMMENT ON COLUMN public.forex_rate_history.base_currency IS
    '3-letter ISO 4217 base currency (uppercase). Convention: rate = X means 1 base = X quote.';

COMMENT ON COLUMN public.forex_rate_history.quote_currency IS
    '3-letter ISO 4217 quote currency (uppercase).';

COMMENT ON COLUMN public.forex_rate_history.rate_date IS
    'Requested date (cache key). May differ from actual_date due to weekend/holiday walk-back.';

COMMENT ON COLUMN public.forex_rate_history.rate IS
    'Historical rate: 1 base = rate quote units. Precision: 20 digits, 10 decimal places.';

COMMENT ON COLUMN public.forex_rate_history.source IS
    'Data source: ecb (primary, EUR-pivot via ECB SDW), yfinance (fallback), manual (trivial self-pairs).';

COMMENT ON COLUMN public.forex_rate_history.actual_date IS
    'Date of the actual observation used (may be earlier than rate_date due to weekend/holiday walk-back).';

COMMENT ON COLUMN public.forex_rate_history.fetched_at IS
    'Timestamp when this row was first fetched from upstream. Immutable thereafter.';

-- RLS
ALTER TABLE public.forex_rate_history ENABLE ROW LEVEL SECURITY;

-- Authenticated users can read all rates (global cache — not user-scoped)
CREATE POLICY forex_history_read_authenticated
    ON public.forex_rate_history
    FOR SELECT
    TO authenticated
    USING (true);

-- INSERT / UPDATE / DELETE: service_role only (no policy = denied for anon/authenticated)
-- Backend uses service_role key from SUPABASE_SERVICE_ROLE_KEY env var.

-- Grant service_role INSERT and UPDATE access explicitly
GRANT INSERT, UPDATE ON public.forex_rate_history TO service_role;
