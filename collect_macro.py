
from logger_config import get_logger
log = get_logger(__name__)
#!/usr/bin/env python3
"""
글로벌 매크로 데이터 수집 스크립트
- 미국 지수 (S&P500, 나스닥, 다우, VIX)
- 미 10년 국채 금리
- 국내 지수 (코스피, 코스닥, 코스피200)
- 환율 (USD, JPY, EUR, CNY)
- 원자재 (WTI, 금, 천연가스, 구리)

의존: yfinance, supabase
설치: pip install yfinance --break-system-packages
"""

import os, sys, logging, time
from datetime import date, datetime, timezone
from dotenv import load_dotenv
load_dotenv()

s [%(levelname)s] %(message)s'
)


try:
    import yfinance as yf
except ImportError:
    log.error("yfinance 미설치 — pip install yfinance --break-system-packages")
    sys.exit(1)

try:
    from supabase import create_client
    sb = create_client(os.getenv('SB_URL'), os.getenv('SB_SERVICE_KEY'))
except Exception as e:
    log.error(f"Supabase 연결 실패: {e}")
    sys.exit(1)


# ── 티커 정의 ──────────────────────────────────────────────────
TICKERS = {
    # 미국 지수
    'sp500':    '^GSPC',
    'nasdaq':   '^IXIC',
    'dow':      '^DJI',
    'vix':      '^VIX',
    'us10y':    '^TNX',   # 미 10년 국채 금리

    # 미국 지수 선물
    'sp500_fut':  'ES=F',   # S&P500 선물
    'nasdaq_fut': 'NQ=F',   # 나스닥100 선물
    'dow_fut':    'YM=F',   # 다우 선물

    # 암호화폐
    'bitcoin':    'BTC-USD',  # 비트코인

    # 국내 지수
    'kospi':    '^KS11',
    'kosdaq':   '^KQ11',
    'kospi200': '^KS200',

    # 환율 (1USD 기준)
    'usd_krw_raw': 'KRW=X',     # USD/KRW
    'jpy_raw':     'JPY=X',     # USD/JPY (역수로 변환)
    'eur_raw':     'EURUSD=X',  # EUR/USD (KRW 환산)
    'cny_raw':     'CNY=X',     # USD/CNY (역수로 KRW 환산)

    # 원자재
    'wti':      'CL=F',    # WTI 원유 선물
    'gold':     'GC=F',    # 금 선물
    'gas':      'NG=F',    # 천연가스 선물
    'copper':   'HG=F',    # 구리 선물

}


def fetch_price_and_chg(ticker_sym: str) -> tuple:
    """
    yfinance에서 현재가 + 전일 대비 등락률 + 실제 데이터 날짜 반환
    returns: (price, chg_pct, data_date) or (None, None, None)
    """
    import math
    try:
        t = yf.Ticker(ticker_sym)
        hist = t.history(period='10d')
        if hist.empty:
            return None, None, None
        closes = hist['Close'].dropna()
        if len(closes) < 2:
            return None, None, None
        price     = float(closes.iloc[-1])
        prev      = float(closes.iloc[-2])
        data_date = closes.index[-1].strftime('%Y-%m-%d')
        if math.isnan(price) or math.isnan(prev):
            return None, None, None
        chg = round((price - prev) / prev * 100, 2) if prev else None
        return round(price, 4), chg, data_date
    except Exception as e:
        log.debug(f"[{ticker_sym}] 조회 실패: {e}")
        return None, None, None


def collect_all() -> dict:
    """모든 지표 수집"""
    result = {}

    log.info("매크로 데이터 수집 시작...")

    latest_dates = {}  # 티커별 실제 데이터 날짜 추적

    for col, sym in TICKERS.items():
        price, chg, data_date = fetch_price_and_chg(sym)
        result[col] = price
        result[col + '_chg_raw'] = chg
        if data_date:
            latest_dates[col] = data_date
        log.info(f"  {col:15} {sym:12} → {price} ({chg}%) [{data_date}]")
        time.sleep(0.3)

    # 휴장일 감지: 코스피 데이터가 오늘 또는 어제(전 거래일) 날짜면 정상
    # yfinance는 당일 장 종료 후 수 시간 후 반영되므로 전일 데이터도 허용
    from datetime import datetime, timedelta, timezone
    kst_now   = datetime.now(timezone.utc) + timedelta(hours=9)
    kst_today = kst_now.date().isoformat()
    kst_yesterday = (kst_now.date() - timedelta(days=1)).isoformat()
    kst_2daysago  = (kst_now.date() - timedelta(days=2)).isoformat()
    # 주말 포함 최근 3일을 허용 (금요일 장 → 토/일에도 유효)
    valid_dates = {kst_today, kst_yesterday, kst_2daysago}
    kospi_date = latest_dates.get('kospi')
    if kospi_date and kospi_date not in valid_dates:
        log.warning(f"⚠️  코스피 최신 데이터 날짜: {kospi_date} (KST 오늘: {kst_today})")
        log.warning(f"   오늘 국내 휴장일로 판단 — kospi/kosdaq/kospi200 수집값 제거 (이전 DB값 유지)")
        result['kospi']    = None
        result['kosdaq']   = None
        result['kospi200'] = None
        result['kospi_chg_raw']    = None
        result['kosdaq_chg_raw']   = None
        result['kospi200_chg_raw'] = None

    # ── 환율 변환 ──────────────────────────────────────────────
    usd_krw = result.get('usd_krw_raw')  # 1 USD = N KRW

    # USD/KRW
    result['usd_krw']     = round(usd_krw, 2) if usd_krw else None
    result['usd_krw_chg'] = result.pop('usd_krw_raw_chg_raw', None)

    # JPY/KRW: 1 USD = N JPY → 100엔 = (100/N)*usd_krw
    jpy = result.get('jpy_raw')  # USD/JPY
    if jpy and usd_krw:
        jpy_krw = round(usd_krw / jpy * 100, 2)
        result['jpy_krw'] = jpy_krw
        result['jpy_krw_chg'] = result.pop('jpy_raw_chg_raw', None)
    else:
        result['jpy_krw'] = None
        result['jpy_krw_chg'] = None

    # EUR/KRW: EUR/USD × USD/KRW
    eur = result.get('eur_raw')  # EUR/USD
    if eur and usd_krw:
        result['eur_krw'] = round(eur * usd_krw, 2)
        result['eur_krw_chg'] = result.pop('eur_raw_chg_raw', None)
    else:
        result['eur_krw'] = None
        result['eur_krw_chg'] = None

    # CNY/KRW: 1 USD = N CNY → 1 CNY = usd_krw/N
    cny = result.get('cny_raw')  # USD/CNY
    if cny and usd_krw:
        result['cny_krw'] = round(usd_krw / cny, 4)
        result['cny_krw_chg'] = result.pop('cny_raw_chg_raw', None)
    else:
        result['cny_krw'] = None
        result['cny_krw_chg'] = None

    # 불필요한 raw 키 제거
    for k in ['usd_krw_raw', 'jpy_raw', 'eur_raw', 'cny_raw']:
        result.pop(k, None)
        result.pop(f'{k}_chg_raw', None)

    # 나머지 _chg_raw → _chg 로 rename
    to_rename = [(k, k.replace('_chg_raw', '_chg'))
                 for k in list(result.keys()) if k.endswith('_chg_raw')]
    for old, new in to_rename:
        result[new] = result.pop(old)

    return result


def save_to_db(data: dict, force: bool = False) -> bool:
    """Supabase macro_data 테이블에 upsert"""
    import math
    today = date.today().isoformat()
    # None과 nan 모두 제거
    payload = {k: v for k, v in data.items()
               if v is not None and not (isinstance(v, float) and math.isnan(v))}
    payload['base_date']  = today
    payload['updated_at'] = datetime.now(timezone.utc).isoformat()

    try:
        res = sb.table('macro_data').upsert(payload, on_conflict='base_date').execute()
        log.info(f"✅ macro_data 저장 완료 ({today}) — {len(payload)}개 컬럼")
        return True
    except Exception as e:
        log.error(f"❌ DB 저장 실패: {e}")
        return False


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='휴장일에도 강제 저장')
    args = parser.parse_args()

    data = collect_all()
    save_to_db(data, force=args.force)
