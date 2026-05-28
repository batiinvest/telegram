-- ══════════════════════════════════════════════════════════════
-- market_investment_summary — 투자포인트 요약 테이블
-- Supabase 대시보드 > SQL Editor 에서 실행
-- ══════════════════════════════════════════════════════════════

-- 1. 테이블 생성
CREATE TABLE IF NOT EXISTS market_investment_summary (
  id                bigserial PRIMARY KEY,
  market_date       date        NOT NULL,
  market_type       text        NOT NULL DEFAULT 'KR',
  one_line_summary  text,
  flow_summary      text,   -- JSON string: 핵심 흐름 데이터
  key_points        text,   -- JSON string: 투자포인트 배열
  risk_factors      text,   -- JSON string: 리스크 요인 배열
  watch_events      text,   -- JSON string: 확인 이벤트 배열
  strong_industries text,   -- JSON string: 강세 산업 배열
  weak_industries   text,   -- JSON string: 약세 산업 배열
  top_stocks        text,   -- JSON string: 주목 종목 배열
  data_basis        text,   -- 데이터 기준 설명
  generated_at      timestamptz DEFAULT now(),
  UNIQUE (market_date, market_type)
);

-- 2. RLS 활성화
ALTER TABLE market_investment_summary ENABLE ROW LEVEL SECURITY;

-- 3. 기존 정책 정리 (재실행 시 오류 방지)
DROP POLICY IF EXISTS "market_summary_read"   ON market_investment_summary;
DROP POLICY IF EXISTS "market_summary_insert" ON market_investment_summary;
DROP POLICY IF EXISTS "market_summary_update" ON market_investment_summary;

-- 4. 읽기: 비로그인 포함 전체 허용
CREATE POLICY "market_summary_read"
  ON market_investment_summary
  FOR SELECT
  TO public
  USING (true);

-- 5. 쓰기(INSERT): 로그인된 사용자만 허용
--    대시보드 'DB 저장' 버튼은 Supabase 세션 JWT를 자동 포함하므로
--    authenticated 역할로 실행됨
CREATE POLICY "market_summary_insert"
  ON market_investment_summary
  FOR INSERT
  TO authenticated
  WITH CHECK (true);

-- 6. 수정(UPDATE / upsert): 로그인된 사용자만 허용
CREATE POLICY "market_summary_update"
  ON market_investment_summary
  FOR UPDATE
  TO authenticated
  USING (true)
  WITH CHECK (true);

-- 확인
SELECT tablename, rowsecurity
FROM pg_tables
WHERE tablename = 'market_investment_summary';
