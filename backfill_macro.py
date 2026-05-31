
from logger_config import get_logger
log = get_logger(__name__)
#!/usr/bin/env python3
"""
과거 매크로 데이터 일괄 수집 스크립트
yfinance에서 날짜별로 가져와 macro_data 테이블에 저장

사용법:
  python3 backfill_macro.py          # 기본: 3달(90일)
  python3 backfill_macro.py --days 180  # 6달
"""

import os, sys, math, time, argparse, logging
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()


try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("pip install yfinance pandas")
    sys.exit(1)

try:
    from db_client import get_supabase_client
    sb = get_supabase_client()
except Exception as e:
    log.error(f"Supabase 연결 실패: {e}"); sys.exit(1)

# ── 티커 정의 (collect_macro.py와 동일) ──
TICKERS = {
    'sp500':    '^GSPC',
    'nasdaq':   '^IXIC',
    'dow':      '^DJI',
    'vix':      '^VIX',
    'us10y':    '^TNX',
    'kospi':    '^KS11',
    'kosdaq':   '^KQ11',
    'kospi200': '^KS200',
    'usd_krw':  'KRW=X',
    'jpy_raw':  'JPY=X',
    'eur_raw':  'EURUSD=X',
    'cny_raw':  'CNY=X',
    'wti':      'CL=F',
    'gold':     'GC=F',
    'gas':      'NG=F',
    'copper':   'HG=F',
}


def fetch_history(days: int) -> dict:
    """
    모든 티커의 날짜별 종가 일괄 조회
    반환: {date_str: {col: value}}
    """
    period = f'{days + 30}d'  # 여유분 추가
    date_map = {}  # {date_str: {col: value}}

    for col, sym in TICKERS.items():
        log.info(f"  [{sym}] 조회 중...")
        try:
            t = yf.Ticker(sym)
            hist = t.history(period=period)
            if hist.empty:
                log.warning(f"  [{sym}] 데이터 없음")
                continue

            for dt, row in hist.iterrows():
                d_str = dt.strftime('%Y-%m-%d')
                if d_str not in date_map:
                    date_map[d_str] = {}
                val = float(row['Close'])
                if not math.isnan(val):
                    date_map[d_str][col] = round(val, 4)
        except Exception as e:
            log.error(f"  [{sym}] 실패: {e}")
        time.sleep(0.3)

    return date_map


def build_payload(d_str: str, today_data: dict, prev_data: dict) -> dict:
    """날짜별 payload 구성 (등락률 계산 포함)"""
    usd_krw = today_data.get('usd_krw')
    jpy     = today_data.get('jpy_raw')
    eur     = today_data.get('eur_raw')
    cny     = today_data.get('cny_raw')

    payload = {'base_date': d_str}

    # 직접 매핑 컬럼
    direct = ['sp500','nasdaq','dow','vix','us10y','kospi','kosdaq','kospi200','wti','gold','gas','copper',
]
    for col in direct:
        v = today_data.get(col)
        if v is not None:
            payload[col] = v
            prev = prev_data.get(col)
            if prev:
                payload[f'{col}_chg'] = round((v - prev) / abs(prev) * 100, 2)

    # 환율 변환
    if usd_krw:
        payload['usd_krw'] = round(usd_krw, 2)
        prev_usd = prev_data.get('usd_krw')
        if prev_usd:
            payload['usd_krw_chg'] = round((usd_krw - prev_usd) / abs(prev_usd) * 100, 2)

    if jpy and usd_krw:
        jpy_krw = round(usd_krw / jpy * 100, 2)
        payload['jpy_krw'] = jpy_krw
        prev_jpy = prev_data.get('jpy_raw')
        prev_usd_p = prev_data.get('usd_krw')
        if prev_jpy and prev_usd_p:
            prev_jpy_krw = prev_usd_p / prev_jpy * 100
            payload['jpy_krw_chg'] = round((jpy_krw - prev_jpy_krw) / abs(prev_jpy_krw) * 100, 2)

    if eur and usd_krw:
        eur_krw = round(eur * usd_krw, 2)
        payload['eur_krw'] = eur_krw
        prev_eur = prev_data.get('eur_raw')
        prev_usd_p = prev_data.get('usd_krw')
        if prev_eur and prev_usd_p:
            prev_eur_krw = prev_eur * prev_usd_p
            payload['eur_krw_chg'] = round((eur_krw - prev_eur_krw) / abs(prev_eur_krw) * 100, 2)

    if cny and usd_krw:
        cny_krw = round(usd_krw / cny, 4)
        payload['cny_krw'] = cny_krw
        prev_cny = prev_data.get('cny_raw')
        prev_usd_p = prev_data.get('usd_krw')
        if prev_cny and prev_usd_p:
            prev_cny_krw = prev_usd_p / prev_cny
            payload['cny_krw_chg'] = round((cny_krw - prev_cny_krw) / abs(prev_cny_krw) * 100, 2)

    payload['updated_at'] = datetime.now(timezone.utc).isoformat()
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=90, help='수집 기간 (기본: 90일)')
    args = parser.parse_args()

    log.info(f"=== 과거 {args.days}일 매크로 데이터 수집 시작 ===")

    # 전체 히스토리 조회
    date_map = fetch_history(args.days)

    # 날짜 범위 필터
    end_date   = date.today()
    start_date = end_date - timedelta(days=args.days)
    target_dates = sorted([
        d for d in date_map.keys()
        if start_date.isoformat() <= d <= end_date.isoformat()
    ])

    log.info(f"수집 날짜: {target_dates[0] if target_dates else '없음'} ~ {target_dates[-1] if target_dates else '없음'} ({len(target_dates)}일)")

    # 날짜순으로 upsert
    ok = 0
    for i, d_str in enumerate(target_dates):
        today_data = date_map[d_str]
        # 전날 데이터 (등락률 계산용)
        prev_data = date_map.get(target_dates[i-1]) if i > 0 else {}

        payload = build_payload(d_str, today_data, prev_data)
        if len(payload) <= 2:  # base_date, updated_at만 있으면 스킵
            continue

        try:
            sb.table('macro_data').upsert(payload, on_conflict='base_date').execute()
            log.info(f"  ✅ {d_str} — {len(payload)-2}개 지표 저장")
            ok += 1
        except Exception as e:
            log.error(f"  ❌ {d_str} 저장 실패: {e}")

    log.info(f"=== 완료: {ok}/{len(target_dates)}일 저장 ===")


if __name__ == '__main__':
    main()
