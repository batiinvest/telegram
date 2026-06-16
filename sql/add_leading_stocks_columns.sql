-- ============================================================
--  leading_stocks 테이블 — 신규 스코어 컬럼 추가
--  leading_stocks_generator.py가 저장을 시도하지만 테이블에 없어
--  매일 17:30 자동 작업이 42703(column does not exist) 에러로
--  11일간(2026-06-05 이후) 조용히 실패하던 문제 해결.
--  실행: Supabase SQL Editor → 실행
-- ============================================================

ALTER TABLE leading_stocks
    ADD COLUMN IF NOT EXISTS sector_strength SMALLINT,   -- 업종 상대강도 점수 (max 20)
    ADD COLUMN IF NOT EXISTS entry_price      NUMERIC,   -- 백테스트용 진입가 (= 당일 종가)
    ADD COLUMN IF NOT EXISTS price_chg_60d    NUMERIC;   -- 60일 수익률 (%)

COMMENT ON COLUMN leading_stocks.sector_strength IS '업종 평균 대비 초과 수익률 백분위 점수 (max 20)';
COMMENT ON COLUMN leading_stocks.entry_price      IS '스코어 산정 당일 종가 — 백테스트 진입가로 재사용';
COMMENT ON COLUMN leading_stocks.price_chg_60d    IS '60거래일 누적 수익률 (%)';
