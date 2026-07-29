
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

import time
from datetime import date, datetime, timezone
from dotenv import load_dotenv
load_dotenv()



# import 시점 sys.exit 금지 — run_all이 잡 안에서 import하므로 SystemExit가
# except Exception을 뚫고 스케줄러 스레드를 죽인다. 실패는 None으로 두고 실행 시 검증.
try:
    import yfinance as yf
except ImportError:
    yf = None
    log.error("yfinance 미설치 — pip install yfinance --break-system-packages")

try:
    from db_client import get_supabase_client
    sb = get_supabase_client()
except Exception as e:
    sb = None
    log.error(f"Supabase 연결 실패: {e}")


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
        from managers import kis_auth
        from datetime import datetime, timedelta, timezone

        kst_now   = datetime.now(timezone.utc) + timedelta(hours=9)
        end_date  = kst_now.strftime('%Y%m%d')
        start_date = (kst_now - timedelta(days=10)).strftime('%Y%m%d')  # 최근 10일치 조회

        params = {
            "fid_cond_mrkt_div_code": "U",   # U = 지수
            "fid_input_iscd":          iscd,
            "fid_input_date_1":        start_date,
            "fid_input_date_2":        end_date,
            "fid_period_div_code":     "D",
        }
        body = kis_auth.kis_get("FHKUP03500100",
                                "quotations/inquire-index-daily-price",
                                params, custtype="P")
        if not body:
            log.warning(f"[KIS 지수] 응답 없음 — 토큰/네트워크 (iscd={iscd})")
            return None, None
        log.debug(f"[KIS 지수 raw] iscd={iscd} keys={list(body.keys())} rt_cd={body.get('rt_cd')} msg={body.get('msg1','')}")

        # output1 = 최신 요약(지수 현재가 + 전일대비율). output2(일별 차트행)에는
        # 등락률(bstp_nmix_prdy_ctrt)이 없어 output2에서 읽으면 항상 0/None → output1 사용.
        o1 = body.get('output1') or {}
        if isinstance(o1, list):
            o1 = o1[0] if o1 else {}
        price = float(o1.get('bstp_nmix_prpr') or 0) or None
        _rate = o1.get('bstp_nmix_prdy_ctrt')
        chg = round(float(_rate), 2) if _rate not in (None, '') else None

        # 폴백: output1 비면 output2 일별행으로 가격·전일대비 계산
        if price is None or chg is None:
            o2 = body.get('output2') or []
            if isinstance(o2, dict):
                o2 = [o2]
            if price is None and o2:
                price = float(o2[0].get('bstp_nmix_prpr') or 0) or None
            if chg is None and len(o2) >= 2:
                _cur  = float(o2[0].get('bstp_nmix_prpr') or 0)
                _prev = float(o2[1].get('bstp_nmix_prpr') or 0)
                if _cur and _prev:
                    chg = round((_cur - _prev) / _prev * 100, 2)

        if not price:
            log.warning(f"[KIS 지수] 가격 없음 (iscd={iscd})")
            return None, None
        log.info(f"  [KIS 지수] iscd={iscd} → {price} ({chg}%)")
        return round(price, 2), chg
    except Exception as e:
        log.warning(f"[KIS 지수] 조회 실패 iscd={iscd}: {e}")
        return None, None


def collect_all() -> dict:
    """모든 지표 수집"""
    if yf is None:
        raise RuntimeError("yfinance 미설치 — 매크로 수집 불가")
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

    # 등락률은 원화 환산 후 재계산 — 원본 티커 등락률(USD/JPY·EUR/USD·USD/CNY)을
    # 그대로 쓰면 USD/KRW 변동분이 빠지고, 역수 통화(엔·위안)는 부호까지 뒤집힌다.
    def _prev(now, chg):
        if now is None or chg is None:
            return None
        d = 1.0 + chg / 100.0
        return now / d if d != 0 else None

    def _pct(now, prev):
        if now is None or prev in (None, 0):
            return None
        return round((now - prev) / prev * 100, 2)

    usd_krw_chg  = result.get('usd_krw_raw_chg_raw')
    usd_krw_prev = _prev(usd_krw, usd_krw_chg)

    # USD/KRW (직접 시세 — 등락률 그대로)
    result['usd_krw']     = round(usd_krw, 2) if usd_krw else None
    result['usd_krw_chg'] = usd_krw_chg

    # JPY/KRW: 100엔 = usd_krw / (USD/JPY) * 100
    jpy = result.get('jpy_raw')  # USD/JPY
    if jpy and usd_krw:
        result['jpy_krw'] = round(usd_krw / jpy * 100, 2)
        _jp = _prev(jpy, result.get('jpy_raw_chg_raw'))
        _pl = (usd_krw_prev / _jp * 100) if (usd_krw_prev and _jp) else None
        result['jpy_krw_chg'] = _pct(usd_krw / jpy * 100, _pl)
    else:
        result['jpy_krw'] = None
        result['jpy_krw_chg'] = None

    # EUR/KRW: (EUR/USD) × usd_krw
    eur = result.get('eur_raw')  # EUR/USD
    if eur and usd_krw:
        result['eur_krw'] = round(eur * usd_krw, 2)
        _ep = _prev(eur, result.get('eur_raw_chg_raw'))
        _pl = (_ep * usd_krw_prev) if (_ep and usd_krw_prev) else None
        result['eur_krw_chg'] = _pct(eur * usd_krw, _pl)
    else:
        result['eur_krw'] = None
        result['eur_krw_chg'] = None

    # CNY/KRW: 1 CNY = usd_krw / (USD/CNY)
    cny = result.get('cny_raw')  # USD/CNY
    if cny and usd_krw:
        result['cny_krw'] = round(usd_krw / cny, 4)
        _cp = _prev(cny, result.get('cny_raw_chg_raw'))
        _pl = (usd_krw_prev / _cp) if (usd_krw_prev and _cp) else None
        result['cny_krw_chg'] = _pct(usd_krw / cny, _pl)
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


def fetch_us_sectors() -> tuple:
    """간밤 미국 섹터 ETF 등락 → 우리 산업 매핑 평균 (아침 브리핑 전용, 라이브).
    us_etf_map(산업↔ETF)로 배치 조회 후 산업별 등가중 평균. save_to_db 경로와 분리 —
    macro_data 테이블에 없는 컬럼이라 collect_all 결과에 넣지 않는다.
    반환: ({industry: chg_pct}, 'YYYY-MM-DD' 미국 세션일) 또는 ({}, None)."""
    if yf is None or sb is None:
        return {}, None
    try:
        rows = sb.table("us_etf_map").select("industry,ticker").execute().data or []
    except Exception as e:
        log.warning(f"[미국섹터] us_etf_map 조회 실패: {e}")
        return {}, None
    if not rows:
        return {}, None
    ind_tickers = {}
    for r in rows:
        ind_tickers.setdefault(r["industry"], []).append(r["ticker"])
    syms = sorted({t for ts in ind_tickers.values() for t in ts})
    try:
        df = yf.download(syms, period="5d", interval="1d",
                         auto_adjust=False, progress=False, threads=True)
        closes = df["Close"]
    except Exception as e:
        log.warning(f"[미국섹터] yf.download 실패: {e}")
        return {}, None
    session_date = None
    try:
        if len(df.index):
            session_date = df.index[-1].strftime("%Y-%m-%d")
    except Exception:
        pass
    chg = {}
    for sym in syms:
        try:
            ser = closes[sym].dropna()
            if len(ser) >= 2 and ser.iloc[-2] > 0:
                chg[sym] = (ser.iloc[-1] - ser.iloc[-2]) / ser.iloc[-2] * 100
        except Exception:
            continue
    out = {}
    for ind, ts in ind_tickers.items():
        vals = [chg[t] for t in ts if t in chg]
        if vals:
            out[ind] = round(sum(vals) / len(vals), 2)
    log.info(f"[미국섹터] {len(out)}개 산업 / {len(chg)}개 티커 (세션 {session_date})")
    return out, session_date


def save_to_db(data: dict, force: bool = False) -> bool:
    """Supabase macro_data 테이블에 upsert"""
    if sb is None:
        log.error("Supabase 미연결 — macro_data 저장 스킵")
        return False
    import math
    today = date.today().isoformat()
    # None과 nan 모두 제거
    payload = {k: v for k, v in data.items()
               if v is not None and not (isinstance(v, float) and math.isnan(v))}
    payload['base_date']  = today
    payload['updated_at'] = datetime.now(timezone.utc).isoformat()

    try:
        sb.table('macro_data').upsert(payload, on_conflict='base_date').execute()
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
