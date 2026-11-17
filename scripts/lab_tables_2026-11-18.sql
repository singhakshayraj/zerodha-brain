-- [P-70] Intraday Strategy Lab tables. Applied to prod via Supabase MCP apply_migration 'lab_tables'.
-- RLS on, no policies: service_role only (same as the other brain tables).
create table if not exists lab_strategies (
  id text primary key,
  arch text not null,
  params jsonb not null,
  rule text not null,
  spec_hash text not null,
  registered_at timestamptz not null default now(),
  status text not null default 'running'
);
create table if not exists lab_trades (
  id bigserial primary key,
  trade_date date not null,
  strategy_id text not null references lab_strategies(id),
  symbol text not null,
  side text not null,
  qty int not null,
  entry_ts timestamptz not null,
  exit_ts timestamptz,
  entry_price numeric not null,
  exit_price numeric,
  stop numeric not null,
  target numeric,
  slip_bps numeric not null,
  risk_inr numeric not null,
  entry_charges numeric not null,
  entry_slip_inr numeric not null,
  entry_ref numeric not null,
  status text not null default 'OPEN',
  exit_reason text,
  gross_inr numeric, charges_inr numeric, slippage_inr numeric, net_inr numeric,
  gross_r numeric, cost_r numeric, net_r numeric
);
create index if not exists lab_trades_date_status on lab_trades (trade_date, status);
create index if not exists lab_trades_strategy_date on lab_trades (strategy_id, trade_date);
create table if not exists lab_daily (
  trade_date date not null,
  strategy_id text not null references lab_strategies(id),
  n int not null, wins int not null,
  gross_inr numeric, charges_inr numeric, slippage_inr numeric, net_inr numeric,
  net_win_inr numeric, net_loss_inr numeric,
  sum_gross_r numeric, sum_cost_r numeric, sum_net_r numeric,
  cum_n int, cum_days int, cum_net_inr numeric, cum_net_r numeric, pf numeric,
  win_rate numeric, mean_net_inr_day numeric, t_day numeric, sprt_llr numeric, sprt_state text,
  primary key (trade_date, strategy_id)
);
create table if not exists lab_refusals (
  trade_date date not null,
  strategy_id text not null,
  reason text not null,
  count int not null,
  primary key (trade_date, strategy_id, reason)
);
alter table lab_strategies enable row level security;
alter table lab_trades enable row level security;
alter table lab_daily enable row level security;
alter table lab_refusals enable row level security;
