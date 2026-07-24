-- 종목별 저녁 요약 기능 (뉴스 영속화 + 생성된 요약 저장)
-- Supabase SQL 에디터에서 실행.
--
-- 배경: 뉴스는 지금까지 텔레그램 발송만 하고 DB에 남지 않았다(중복관리는 sent_news.txt뿐).
-- 저녁에 네이버로 재조회하는 방식은 불가능 — 뉴스봇이 종일 폴링해 API 키 10개 중
-- 7개가 일일 한도(25,000)를 소진한다. 따라서 '발송 시점 적재'가 유일한 확보 경로다.

-- ── 1. 뉴스 아카이브 ────────────────────────────────────────────────
create table if not exists public.daily_news (
  id           bigint generated always as identity primary key,
  base_date    date not null,               -- KST 발행일 (요약 조회 기준)
  corp_name    text not null,
  stock_code   text,
  industry     text,
  title        text not null,
  description  text,
  link         text not null,
  source       text,                        -- 매체 도메인
  published_at timestamptz,
  created_at   timestamptz not null default now(),
  -- 같은 기사가 여러 종목에 걸릴 수 있어 종목까지 포함해 유니크
  constraint daily_news_uniq unique (base_date, corp_name, link)
);
create index if not exists daily_news_date_corp_idx
  on public.daily_news (base_date, corp_name);
create index if not exists daily_news_code_idx
  on public.daily_news (stock_code, base_date desc);

-- ── 2. 생성된 종목별 저녁 요약 ──────────────────────────────────────
create table if not exists public.daily_summaries (
  id             bigint generated always as identity primary key,
  base_date      date not null,
  corp_name      text not null,
  stock_code     text,
  industry       text,
  disclosure_cnt int  not null default 0,
  news_cnt       int  not null default 0,   -- 이벤트 군집화 후 건수
  is_major       boolean not null default false,
  ai_summary     text,                      -- 하이브리드: 대형/긴급 종목만 채워짐
  items          jsonb not null default '[]'::jsonb,  -- 공시·뉴스 상세(프론트 카드용)
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  constraint daily_summaries_uniq unique (base_date, corp_name)
);
create index if not exists daily_summaries_date_idx
  on public.daily_summaries (base_date desc);
create index if not exists daily_summaries_code_idx
  on public.daily_summaries (stock_code, base_date desc);

-- ── 3. RLS ──────────────────────────────────────────────────────────
-- 프론트(anon 키)가 종목 리포트 카드에서 읽으므로 읽기 정책이 필요하다.
-- 쓰기는 서비스 키(수집·생성 스크립트) 전용 — 정책을 주지 않으면 anon은 못 쓴다.
alter table public.daily_news      enable row level security;
alter table public.daily_summaries enable row level security;

drop policy if exists daily_news_read on public.daily_news;
create policy daily_news_read on public.daily_news
  for select using (true);

drop policy if exists daily_summaries_read on public.daily_summaries;
create policy daily_summaries_read on public.daily_summaries
  for select using (true);

-- (선택) 보관기간 정리가 필요해지면:
--   delete from public.daily_news      where base_date < current_date - interval '1 year';
--   delete from public.daily_summaries where base_date < current_date - interval '1 year';
