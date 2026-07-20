-- 추정실적 스냅샷 + 상향 감지 (collect_estimates.py, 매일 18:40 job_collect_estimates)
-- Supabase SQL 에디터에서 실행. unique 인덱스는 batch_upsert on_conflict 키와 일치해야 함.

create table if not exists public.consensus_estimates (
  id             bigint generated always as identity primary key,
  stock_code     text not null,
  stock_name     text,
  fiscal_period  text not null,            -- '2026.12' (E 제거된 회계연도)
  is_estimate    boolean not null default false,
  revenue        double precision,         -- 억원
  revenue_yoy    double precision,         -- %
  op_profit      double precision,
  op_profit_yoy  double precision,
  net_profit     double precision,
  net_profit_yoy double precision,
  eps            double precision,
  per            double precision,
  roe            double precision,
  est_date       date not null,            -- 애널리스트 추정일 (스냅샷 축)
  opinion        text,
  analyst        text,
  collected_at   timestamptz not null default now()
);
create unique index if not exists consensus_estimates_uidx
  on public.consensus_estimates (stock_code, fiscal_period, est_date);

create table if not exists public.estimate_revisions (
  id                   bigint generated always as identity primary key,
  stock_code           text not null,
  stock_name           text,
  fiscal_period        text not null,
  prev_est_date        date not null,
  new_est_date         date not null,
  revenue_prev         double precision,
  revenue_new          double precision,
  revenue_change_pct   double precision,
  op_profit_prev       double precision,
  op_profit_new        double precision,
  op_profit_change_pct double precision,
  detected_at          timestamptz not null default now()
);
create unique index if not exists estimate_revisions_uidx
  on public.estimate_revisions (stock_code, fiscal_period, new_est_date);

-- 읽기: 프론트(anon) 허용 / 쓰기: 서비스 키 전용 (서비스 키는 RLS 우회)
alter table public.consensus_estimates enable row level security;
alter table public.estimate_revisions  enable row level security;
create policy "read consensus_estimates" on public.consensus_estimates for select using (true);
create policy "read estimate_revisions"  on public.estimate_revisions  for select using (true);
