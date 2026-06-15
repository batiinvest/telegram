
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



try:
    import yfinance as yf
except ImportError:
    log.error("yfinance 미설치 — pip install yfinance --break-system-packages")
    sys.exit(1)

try:
    from db_client import get_supabase_client
    sb = get_supabase_client()
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
    1차: fast_info (현재가/전일종가 직접 조회, 가장 정확)
    2차: history(auto_adjust=False) fallback
    returns: (price, chg_pct, data_date) or (None, None, None)
    """
    import math
    from datetime import timezone as _tz
    try:
        t = yf.Ticker(ticker_sym)

        # 1차: fast_info — 실시간/당일 값, 자동조정 없음
        try:
            fi = t.fast_info
            price = fi.last_price
            prev  = fi.previous_close
            if price and prev and not math.isnan(price) and not math.isnan(prev) and prev > 0:
                chg = round((price - prev) / prev * 100, 2)
                # 데이터 날짜: regularMarketTime 있으면 사용, 없으면 오늘(KST)
                from datetime import datetime, timedelta
                try:
                    ts = fi.last_volume  # fast_info에 직접 날짜 없으므로 history 1행으로 날짜만 추출
                except Exception:
                    pass
                hist1 = t.history(period='2d', auto_adjust=False)
                if not hist1.empty:
                    last_idx = hist1.index[-1]
                    # timezone-aware → UTC로 정규화 후 KST 변환
                    if hasattr(last_idx, 'tzinfo') and last_idx.tzinfo is not None:
                        last_idx_utc = last_idx.astimezone(_tz.utc)
                        data_date = (last_idx_utc + timedelta(hours=9)).strftime('%Y-%m-%d')
                    else:
                        data_date = last_idx.strftime('%Y-%m-%d')
                else:
                    from datetime import datetime, timedelta
                    kst_today = (datetime.now(_tz.utc) + timedelta(hours=9)).strftime('%Y-%m-%d')
                    data_date = kst_today
                log.debug(f"[{ticker_sym}] fast_info → {price} ({chg}%) [{data_date}]")
                return round(price, 4), chg, data_date
        except Exception as e:
            log.debug(f"[{ticker_sym}] fast_info 실패, fallback: {e}")

        # 2차: history fallback (auto_adjust=False 로 원시 종가 사용)
        hist = t.history(period='10d', auto_adjust=False)
        if hist.empty:
            return None, None, None
        closes = hist['Close'].dropna()
        if len(closes) < 2:
            return None, None, None
        price = float(closes.iloc[-1])
        prev  = float(closes.iloc[-2])
        if math.isnan(price) or math.isnan(prev) or prev == 0:
            return None, None, None
        last_idx = closes.index[-1]
        if hasattr(last_idx, 'tzinfo') and last_idx.tzinfo is not None:
            from datetime import timedelta
            last_idx_utc = last_idx.astimezone(_tz.utc)
            data_date = (last_idx_utc + timedelta(hours=9)).strftime('%Y-%m-%d')
        else:
            data_date = last_idx.strftime('%Y-%m-%d')
        chg = round((price - prev) / prev * 100, 2)
        return round(price, 4), chg, data_date

    except Exception as e:
        log.debug(f"[{ticker_sym}] 조회 실패: {e}")
        return None, None, None


def fetch_kr_index_kis(iscd: str) -> tuple:
    """
    KIS 일별 지수 차트 API로 최근 거래일 종가 + 등락률 조회
    (장중/장외/주말/휴장일 모두 마지막 거래일 종가 반환)
    iscd: '0001'=코스피, '1001'=코스닥, '2001'=코스피200
    returns: (price, chg_pct) or (None, None)
    """
    try:
        from managers import kis_auth, KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
        from datetime import datetime, timedelta, timezone
        import requests as req

        token = kis_auth.get_token()
        if not token:
            log.warning(f"[KIS 지수] 토큰 없음 (iscd={iscd})")
            return None, None

        kst_now   = datetime.now(timezone.utc) + timedelta(hours=9)
        end_date  = kst_now.strftime('%Y%m%d')
        start_date = (kst_now - timedelta(days=10)).strftime('%Y%m%d')  # 최근 10일치 조회

        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-daily-price"
        headers = {
            "authorization": f"Bearer {token}",
            "appkey":    KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id":     "FHKUP03500100",
            "custtype":  "P",
        }
        params = {
            "fid_cond_mrkt_div_code": "U",   # U = 지수
            "fid_input_iscd":          iscd,
            "fid_input_date_1":        start_date,
            "fid_input_date_2":        end_date,
            "fid_period_div_code":     "D",
        }
        r = req.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        body = r.json()
        log.debug(f"[KIS 지수 raw] iscd={iscd} keys={list(body.keys())} rt_cd={body.get('rt_cd')} msg={body.get('msg1','')}")

        # output2 = 일별 리스트, output = 단일 / 키가 다를 수 있으므로 순서대로 시도
        rows = body.get('output2') or body.get('output1') or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            log.warning(f"[KIS 지수] 데이터 없음 (iscd={iscd}) rt_cd={body.get('rt_cd')} msg={body.get('msg1','')}")
            return None, None

        # 가장 최근 거래일 (첫 번째 행)
        latest = rows[0]
        price = float(latest.get('bstp_nmix_clpr') or latest.get('bstp_nmix_prpr') or 0) or None
        rate  = float(latest.get('bstp_nmix_prdy_ctrt') or 0)
        if not price:
            return None, None
        chg = round(rate, 2)
        stck_bsop_date = latest.get('stck_bsop_date', '')
        log.info(f"  [KIS 지수] iscd={iscd} → {price} ({chg}%) [{stck_bsop_date}]")
        return round(price, 2), chg
    except Exception as e:
        log.warning(f"[KIS 지수] 조회 실패 iscd={iscd}: {e}")
        return None, None


def collect_all() -> dict:
    """모든 지표 수집"""
    result = {}

    log.info("매크로 데이터 수집 시작...")

    # ── 코스피/코스닥/코스피200: KIS API (yfinance보다 정확) ──
    log.info("  [KIS] 국내 지수 수집...")
    kospi_val,   kospi_chg   = fetch_kr_index_kis('0001')
    kosdaq_val,  kosdaq_chg  = fetch_kr_index_kis('1001')
    kospi200_val, kospi200_chg = fetch_kr_index_kis('2001')

    result['kospi']        = kospi_val
    result['kospi_chg']    = kospi_chg
    result['kosdaq']       = kosdaq_val
    result['kosdaq_chg']   = kosdaq_chg
    result['kospi200']     = kospi200_val
    result['kospi200_chg'] = kospi200_chg

    # KIS에서 값을 못 가져온 경우 경고 (휴장일 등)
    if kospi_val is None:
        log.warning("⚠️  코스피 KIS 수집 실패 — 휴장일이거나 토큰 만료")

    latest_dates = {}  # 티커별 실제 데이터 날짜 추적 (해외 지표용)

    # TICKERS에서 국내 지수는 제외 (KIS로 대체)
    KR_INDEX_KEYS = {'kospi', 'kosdaq', 'kospi200'}

    for col, sym in TICKERS.items():
        if col in KR_INDEX_KEYS:
            continue  # KIS로 수집했으므로 스킵
        price, chg, data_date = fetch_price_and_chg(sym)
        result[col] = price
        result[col + '_chg_raw'] = chg
        if data_date:
            latest_dates[col] = data_date
        log.info(f"  {col:15} {sym:12} → {price} ({chg}%) [{data_date}]")
        time.sleep(0.3)

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
