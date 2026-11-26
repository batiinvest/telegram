"""미국 지수·ETF 일별 종가 → us_index_daily (미국 위험신호 · 미국 피어앤그리드 원재료)

[원본] 태린이아빠 Market Dashboard(공개 소스 app.py)
  · '미국 위험신호' canary()            — QQQ · TIP
  · '미국 피어앤그리드 오실레이터' FG_SRC — ^GSPC · ^IXIC · ^VIX · ^TNX · ^FVX · HYG · IEF
  둘 다 yfinance Close(auto_adjust=False — 분할만 반영, 배당은 반영하지 않은 종가)를 쓴다.
  피어앤그리드는 2024-01-01부터의 구간 전체로 MinMax를 잡으므로 시작일을 원본과 같게 둔다.

[계산] 여기서는 원재료만 저장하고 계산은 화면(js/us-market.js)이 한다 — 한국 피어앤그리드
  (collect_fear_greed.py → fear-greed.js)와 같은 방식.
    위험신호 = 종목별 (21 · 63 · 126 · 252거래일 수익률)의 평균. QQQ · TIP 둘 다 양수면 공격, 아니면 방어.
    피어앤그리드 = 125일 이격 · RSI 10일 · (10년 − 5년 금리) · VIX(역방향) · HYG ÷ IEF 각 20%
                  → 구간 MinMax → EMA20, 오실레이터 = MACD(12, 26) − 시그널 9.

[원본 대조 — 2026-11-12, 원본 화면 11-11 미국 종가 기준(화면의 '마지막 업데이트' 시각은 UTC)]
  위험신호      QQQ −1.37% · TIP −2.87% · 방어 모드 — 같음.
  피어앤그리드  S&P500 58.0 / EMA20 57.5 / 오실레이터 0.0025 — 우리 57.98 / 57.50 / 0.00251
                NASDAQ 54.2 / 54.3 / 0.0039               — 우리 54.23 / 54.29 / 0.00394

[저장] 날짜마다 한 줄. 받은 값과 저장된 값이 다른 날만 쓴다(분할로 과거 값이 바뀌어도 따라간다).
  미국 장중에 돌리면 그날 봉은 아직 끝나지 않은 값이라 뺀다(뉴욕 16:15 전).
  미국 휴장일 행은 버린다(us_calendar) — ^VIX는 Cboe 연장 거래로 휴장일에도 봉이 생겨(2026-11-26 26.80 · 05-25 · 09-07)
  그날 행이 S&P500 · NASDAQ 없이 생긴다. 원본 FG_SRC도 전 컬럼 dropna라 이런 날은 빠진다.
  예전엔 휴장일 다음 아침 '종가가 비었습니다'로 실패하고, 그다음 거래일엔 그 행을 새 날로 저장했다.

[시각] 06:40(미국 마감 뒤 — 겨울 06:00 · 여름 05:00 마감) · 08:10(한 번 더).

실행: python3 collect_us_index.py [--dry]
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages
import us_calendar

log = get_logger(__name__)

TABLE = 'us_index_daily'
START = '2024-01-01'           # 원본 FG_SRC start
# 저장 컬럼 → yfinance 티커
SYMS = {
    'spx': '^GSPC', 'ndx': '^IXIC', 'vix': '^VIX', 'tnx': '^TNX', 'fvx': '^FVX',
    'hyg': 'HYG', 'ief': 'IEF', 'qqq': 'QQQ', 'tip': 'TIP',
}
MIN_ROWS = 300                 # 이보다 적게 오면 받기 실패로 본다
NY_CLOSE = (16, 15)            # 이 시각(뉴욕) 전이면 오늘 봉은 진행 중


def _has_schema(sb) -> bool:
    try:
        sb.table(TABLE).select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def build_rows(close, ny) -> tuple:
    """종가 표(날짜 × 야후 티커) → (행 목록 오름차순, 버린 미국 휴장일 목록).
    ny: 지금 뉴욕 시각 — 그날 봉은 16:15 전이면 진행 중이라 뺀다. 값이 하나도 없는 날도 뺀다."""
    import math

    rows, off = [], []
    for ts, rec in close.iterrows():
        day = ts.date()
        if day == ny.date() and (ny.hour, ny.minute) < NY_CLOSE:
            continue           # 오늘 봉은 아직 진행 중
        row = {'base_date': day.isoformat()}
        for col, sym in SYMS.items():
            v = rec.get(sym)
            row[col] = None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 4)
        if not any(row[c] is not None for c in SYMS):
            continue
        if not us_calendar.is_trading_day(day):
            off.append(row['base_date'])   # ^VIX 연장 거래 봉만 있는 휴장일 — 저장하지 않는다
            continue
        rows.append(row)
    return rows, off


def fetch() -> list:
    """yfinance 종가 → [{base_date, spx, …}] 오름차순. 값이 없는 칸은 None. 미국 휴장일 행은 뺀다."""
    import yfinance as yf

    raw = yf.download(list(SYMS.values()), start=START, auto_adjust=False, progress=False, threads=False)
    if raw is None or raw.empty:
        raise RuntimeError('yfinance가 빈 결과를 돌려줬습니다')
    close = raw['Close'] if getattr(raw.columns, 'nlevels', 1) > 1 else raw
    rows, off = build_rows(close, datetime.now(ZoneInfo('America/New_York')))
    if off:
        log.info(f"[미국지수] 미국 휴장일 행 {len(off)}일 버림(^VIX 연장 거래 봉): {', '.join(off[-3:])}")
    if len(rows) < MIN_ROWS:
        raise RuntimeError(f'받은 날짜가 {len(rows)}일뿐입니다(기대 {MIN_ROWS}일 이상)')
    last = rows[-1]
    if last['spx'] is None or last['ndx'] is None:
        raise RuntimeError(f"{last['base_date']} S&P500·NASDAQ 종가가 비었습니다")
    return rows


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 1e-6


def changed_rows(rows: list, have: dict) -> list:
    """저장된 값과 다른 날만."""
    return [r for r in rows
            if r['base_date'] not in have or any(not _same(r[c], have[r['base_date']].get(c)) for c in SYMS)]


def run(dry: bool = False) -> dict:
    sb = get_supabase_client()
    has = _has_schema(sb)
    if not dry and not has:
        log.warning(f'[미국지수] {TABLE} 테이블 없음 — sql/us_indicators.sql을 먼저 실행하세요 (건너뜀)')
        return {'last': None, 'rows': 0, 'written': 0, 'skipped': 'no_table'}
    rows = fetch()
    have = {}
    if has:
        have = {r['base_date']: r for r in fetch_all_pages(
            sb.table(TABLE).select('base_date,' + ','.join(SYMS)).order('base_date'))}
    todo = changed_rows(rows, have)
    if todo and not dry:
        for i in range(0, len(todo), 500):
            sb.table(TABLE).upsert(todo[i:i + 500], on_conflict='base_date').execute()
    last = rows[-1]
    log.info(f"[미국지수] {'dry-run ' if dry else ''}{last['base_date']} S&P500 {last['spx']} · NASDAQ {last['ndx']} · "
             f"VIX {last['vix']} · 받은 {len(rows)}일 · 쓴 {len(todo)}일")
    return {'last': last['base_date'], 'rows': len(rows), 'written': 0 if dry else len(todo)}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 받기만')
    print(run(dry=ap.parse_args().dry))
