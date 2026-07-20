-- 잡 실행 이력 (job_infra._record_job_db가 기록)
-- freshness(19:00)·자동 재처리(19:25)·운영요약(19:50)의 단일 기반.
-- Supabase SQL 에디터에서 실행.

create table if not exists public.job_runs (
  id          bigint generated always as identity primary key,
  run_date    date not null,               -- KST 기준일
  job_name    text not null,
  started_at  timestamptz,
  finished_at timestamptz not null default now(),
  ok          boolean not null,
  error       text,
  elapsed_sec double precision
);
create index if not exists job_runs_date_idx
  on public.job_runs (run_date, job_name);

-- 운영 데이터 — 프론트 노출 불필요. 정책 없이 RLS만 켜서 서비스 키 전용으로.
alter table public.job_runs enable row level security;

-- (선택) 이력 6개월 초과분 정리가 필요해지면:
--   delete from public.job_runs where run_date < current_date - interval '6 months';
