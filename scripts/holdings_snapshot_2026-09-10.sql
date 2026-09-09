-- ============================================================================
-- [Task 2] holdings_snapshot — append-only book captures
-- Reviewable DDL; apply by hand, never by an agent. Idempotent.
--
-- One row per holding per capture; snapshot_id groups a capture. Listed names
-- carry a Kite last_price; fund (NAV-based) holdings carry units + identifier
-- and a NULL price -- valuation is the AMFI NAV join via holding_scheme_map, so
-- an unmapped/unpriced scheme stays NULL and I-8's reconciliation catches the
-- gap rather than a fabricated price hiding it. Append-only: a changed book is
-- a NEW snapshot_id; nothing is overwritten.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.holdings_snapshot (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id     text        NOT NULL,          -- groups one capture
    captured_at     timestamptz NOT NULL,
    identifier      text        NOT NULL,          -- listed: SYMBOL; fund: scheme code
    isin            text,
    instrument_type text        NOT NULL
        CHECK (instrument_type IN ('listed','fund')),
    quantity        numeric,                        -- qty (listed) or units (fund)
    average_price   numeric,
    last_price      numeric,                        -- price/NAV; NULL for funds
    source          text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, identifier)
);
CREATE INDEX IF NOT EXISTS holdings_snapshot_captured_idx
    ON public.holdings_snapshot (captured_at DESC);
CREATE INDEX IF NOT EXISTS holdings_snapshot_sid_idx
    ON public.holdings_snapshot (snapshot_id);
