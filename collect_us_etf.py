
from logger_config import get_logger
log = get_logger(__name__)
"""
collect_us_etf.py — 미국 산업별 ETF 수집 → us_market 테이블 저장

의존: yfinance, supabase
실행: python3 collect_us_etf.py [--backfill DAYS]
"""

import os, sys, logging, time, argparse
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()



try:
    import yfinance as yf
except ImportError:
    log.error("yfinance 미설치 — pip install yfinance --break-system-packages")
    sys.exit(1)

try:
    from db_client import get_supabase_client
    _sb = get_supabase_client()
except Exception as e:
    log.error(f"Supabase 연결 실패: {e}")
    sys.exit(1)

# ── 산업별 ETF 정의 ──────────────────────────────────────────────
US_ETF_MAP = {
    '반도체': ['SOXX', 'SMH', 'XSD'],
    '바이오': ['IBB', 'XBI', 'ARKG'],
    '로봇':   ['BOTZ', 'ROBO'],
    '우주':   ['ARKX', 'UFO', 'ROKT'],
    '2차전지':['LIT', 'BATT', 'DRIV'],
    '소비재': ['XLY', 'ONLN', 'RTH'],
    '엔터':   ['XLC', 'PEJ', 'HERO', 'BNGE'],
    '조선':   ['BOAT', 'SEA', 'BDRY'],
    '테크':   ['VGT', 'XLK', 'IYW'],
    '뷰티':   ['RTH', 'ONLN'],
    '신재생': ['ICLN', 'QCLN', 'PBW', 'RNRG', 'FAN', 'TAN'],
}

# 전체 유니크 티커 목록
ALL_TICKERS = {}
for ind, tickers in US_ETF_MAP.items():
    for t in tickers:
        ALL_TICKERS[t] = ind  # 마지막 산업으로 덮어쓰기 (중복 없도록 별도 관리)

# 중복 티커를 모든 산업에 매핑
TICKER_INDUSTRIES = {}  # { 'ONLN': ['소비재', '뷰티'], ... }
for ind, tickers in US_ETF_MAP.items():
    for t in tickers:
        TICKER_INDUSTRIES.setdefault(t, []).append(ind)


# ── 데이터 수집 ──────────────────────────────────────────────────
def fetch_history(ticker: str, days: int = 10):
    """yfinance에서 최근 N일 히스토리 반환 → {date_str: (close, chg_pct)}"""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=f'{days + 10}d')
        if hist.empty:
            return {}
        closes = hist['Close'].dropna()
        result = {}
        dates = list(closes.index)
        for i in range(1, len(dates)):
            cur  = float(closes.iloc[i])
            prev = float(closes.iloc[i-1])
            d    = dates[i].strftime('%Y-%m-%d')
            chg  = round((cur - prev) / prev * 100, 4) if prev else None
            result[d] = (round(cur, 4), chg)
        return result
    except Exception as e:
        log.warning(f"[{ticker}] 조회 실패: {e}")
        return {}


def collect_and_save(days: int = 5):
    """us_etf_map 테이블에서 티커 목록 조회 후 us_market에 가격 데이터 upsert"""
    # us_etf_map에서 최신 매핑 조회
    try:
        map_res = _sb.table('us_etf_map').select('industry,ticker').execute()
        etf_rows = map_res.data or []
    except Exception as e:
        log.error(f"us_etf_map 조회 실패: {e}")
        etf_rows = []

    # { ticker: [industry, ...] } 빌드
    ticker_industries = {}
    for r in etf_rows:
        ticker_industries.setdefault(r['ticker'], [])
        if r['industry'] not in ticker_industries[r['ticker']]:
            ticker_industries[r['ticker']].append(r['industry'])

    if not ticker_industries:
        log.warning("us_etf_map에 데이터 없음 — 수집 스킵")
        return

    log.info(f"수집 대상: {len(ticker_industries)}개 티커")
    all_rows = []
    seen = set()

    for ticker, industries in ticker_industries.items():
        log.info(f"  수집: {ticker} ({', '.join(industries)})")
        hist = fetch_history(ticker, days=max(days + 5, 15))
        for d, (close, chg) in hist.items():
            for industry in industries:
                key = (d, ticker, industry)
                if key in seen:
                    continue
                seen.add(key)
                all_rows.append({
                    'base_date': d,
                    'ticker':    ticker,
                    'industry':  industry,
                    'close':     close,
                    'chg_pct':   chg,
                })
        time.sleep(0.5)

    if not all_rows:
        log.warning("수집된 데이터 없음")
        return

    # 날짜 필터 (days 기준)
    cutoff = (date.today() - timedelta(days=days + 5)).isoformat()
    all_rows = [r for r in all_rows if r['base_date'] >= cutoff]

    log.info(f"총 {len(all_rows)}건 upsert 시작...")
    chunk = 500
    for i in range(0, len(all_rows), chunk):
        batch = all_rows[i:i+chunk]
        try:
            _sb.table('us_market').upsert(
                batch, on_conflict='base_date,ticker,industry'
            ).execute()
            log.info(f"  ✅ {i+len(batch)}/{len(all_rows)} 저장 완료")
        except Exception as e:
            log.error(f"  ❌ 저장 실패: {e}")

    log.info("✅ us_market 수집 완료")


# ── 메인 ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--backfill', type=int, default=5,
                        help='수집할 거래일 수 (기본 5, 과거채우기: 90 등)')
    args = parser.parse_args()
    collect_and_save(days=args.backfill)
