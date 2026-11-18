"""
collect_kr_monthly.py — 국내 월 리밸런싱 V2.3 (업종지수 Breadth · 코스피 상대강도 · 회복 · 역추세 · 가격 기반 F&G)
→ market_snapshot(kind='kr_monthly')

[원본] 태린이아빠 Market Dashboard '국내시장 월단위 리밸런싱 투자전략'(공개 소스 app.py KR_MONTHLY_REBALANCE_SRC, V2.3)
  매달 마지막 거래일 종가로 업종 26개의 120일선 위 비율(Breadth)을 보고 구간을 나눈다. 다음 거래일부터 적용.
  · 추세(Breadth ≥ 60%): 적격 업종(종가 > 120일선 ∧ 6개월 코스피 대비 상대모멘텀 > 0) RS 상위 3 — 기존 보유는 5위까지 유지, 100%
  · 비추세(40 ~ 60%): 같은 방식으로 고르되 비중은 상위 3 평균 RS <5%·5~12%·12~22%·≥22% → 25·50·75·100%
      V2.3 보정: 코스피 < 200일선 ∧ 6개월 절대모멘텀 < −5% ∧ 252일 고점 대비 ≤ −15%면 −75%p
  · 회복(30 ~ 40%): 직전 6개 월말 상위 3에 들었던 업종 중 지금도 적격이고 20일 고점 대비 −15 ~ −5% 눌린 것 최대 3개에 50%
  · 역추세(< 30%): 코스피 252일 고점 대비 −12·−18·−24·−30% 이하 → 50·75·100·100% 코스피
      V2.1: F&G ≤ 30 ∧ 히스토그램 > 0 ∧ 직전 월말보다 큼 → 0/25/50/75/100 단계에서 한 단계 확대
  · 코스피 가격 기반 F&G = RSI14 25% + 125일 모멘텀 25% + 20일선 이격 20% + 252일 고점 대비 15% + 20일 변동성(역) 15%,
    각각 직전 252거래일 안 백분위. 오실레이터 = EMA5 − EMA20, 시그널 EMA5, 히스토그램 = 오실레이터 − 시그널.
  · 백테스트: 월말 종가 신호 → 다음 거래일 적용, 편도 거래비용 0.1%, 비중은 매일 가격대로 움직임. V2 · V2.1 · V2.3 · 코스피 비교.
  · '이번 달 확정' = 직전 월말 신호, '다음 달 예상' = 최신 거래일을 월말로 친 신호(아직 적용 전).
[자료] WICS 업종지수 26개(wiseindex.com DataCenter, WI100 ~ WI800) + 코스피(야후 ^KS11, 빈 날은 KRX 지수).
  원본 엑셀(DataGuide 'Time Series (Sector)', 2015-12-30 ~)과 같은 지수 — 사용자 보유 원본 엑셀(2026-09-28까지)과 대조:
  거래일 2,633일 모두 같고 값은 0.01 이내(31칸만 0.1% 안쪽 차이). 자료 시작도 원본과 같은 2015-12-30.
  ※ 원본 사이트의 최신판은 업종지수 대신 HTS로 받은 업종 ETF 26개 종가를 쓴다(목록 비공개, 화면엔 13개만 보임) —
     여기서는 원본 엑셀과 같은 업종지수판을 쓴다. 코스피만으로 정해지는 값(F&G · 낙폭 · 200일선 · 6개월 모멘텀)은 원본 화면과 대조.
[돌리는 때] 매일 저녁 · 다음 날 아침 — wiseindex는 장 마감 뒤 저녁에 그날 값을 올린다. 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다.
"""
import argparse
import json
import logging
import math
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone

from db_client import get_supabase_client

log = logging.getLogger(__name__)

TABLE = 'market_snapshot'
KIND = 'kr_monthly'
KEEP_DAYS = 10
DATA_START = '2015-12-30'      # 원본 엑셀 시작일
BENCH = '코스피'
SECTORS = [('WI100', '에너지'), ('WI110', '화학'), ('WI200', '비철,목재등'), ('WI210', '철강'), ('WI220', '건설,건축관련'), ('WI230', '기계'),
           ('WI240', '조선'), ('WI250', '상사,자본재'), ('WI260', '운송'), ('WI300', '자동차'), ('WI310', '화장품,의류,완구'),
           ('WI320', '호텔,레저서비스'), ('WI330', '미디어,교육'), ('WI340', '소매(유통)'), ('WI400', '필수소비재'), ('WI410', '건강관리'),
           ('WI500', '은행'), ('WI510', '증권'), ('WI520', '보험'), ('WI600', '소프트웨어'), ('WI610', 'IT하드웨어'), ('WI620', '반도체'),
           ('WI630', 'IT가전'), ('WI640', '디스플레이'), ('WI700', '통신서비스'), ('WI800', '유틸리티')]
WISE_URL = 'https://www.wiseindex.com/DataCenter/GridData'

# ── 원본 0. 설정 (V2.3) ─────────────────────────────────────────────────
MOMENTUM_DAYS = 126
MA_DAYS = 120
KOSPI_HIGH_DAYS = 252
BREADTH_TREND = 0.60
BREADTH_NON_TREND = 0.40
BREADTH_CONTRARIAN = 0.30
KOSPI_TREND_MA_DAYS = 200
KOSPI_ABS_MOMENTUM_DAYS = 126
NON_TREND_WEAK_MOMENTUM_THRESHOLD = -0.05
NON_TREND_WEAK_DD_THRESHOLD = -0.15
NON_TREND_WEAK_CUT = 0.75
ENTRY_RANK = 3
HOLD_RANK = 5
TRANSACTION_COST = 0.001
RS_CUT_1, RS_CUT_2, RS_CUT_3 = 0.05, 0.12, 0.22
NON_TREND_EXPOSURES = {'LOW': 0.25, 'MID_LOW': 0.50, 'MID_HIGH': 0.75, 'HIGH': 1.00}
RECOVERY_HISTORY_MONTHS = 6
RECOVERY_EXPOSURE = 0.50
RECOVERY_HIGH_DAYS = 20
RECOVERY_DD_MIN, RECOVERY_DD_MAX = -0.15, -0.05
FINAL_OVERLAY_LEVELS = [(-0.30, 1.00), (-0.24, 1.00), (-0.18, 0.75), (-0.12, 0.50)]
FG_RSI_DAYS, FG_MOMENTUM_DAYS, FG_MA_DAYS, FG_VOL_DAYS = 14, 125, 20, 20
FG_HIGH_DAYS, FG_PERCENTILE_DAYS = 252, 252
FG_FAST_EMA, FG_SLOW_EMA, FG_SIGNAL_EMA = 5, 20, 5
FG_OVERSOLD_LEVEL = 30.0
EXPOSURE_STEPS = [0.00, 0.25, 0.50, 0.75, 1.00]
STRATS = (('v2', 'final_v2'), ('v21', 'final_v2_1'), ('v23', 'final_v2_2'))
RECENT_SIGNALS = 12


def _has_schema(sb) -> bool:
    try:
        sb.table(TABLE).select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def _num(v, nd=4):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


# ── 자료 ─────────────────────────────────────────────────────────────────

def fetch_sectors(start=DATA_START, end=None, per=500):
    """wiseindex 업종지수 26개 일별 종가 → DataFrame(날짜 × 업종 이름). 한 번에 13개씩 · 500줄씩(1,000줄 넘으면 오류 페이지로 넘어감)."""
    import pandas as pd
    import requests

    end = end or (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
    frames = []
    for i in range(0, len(SECTORS), 13):
        group = SECTORS[i:i + 13]
        codes = [c for c, _ in group]
        rows, page = [], 1
        while True:
            for attempt in range(3):
                try:
                    r = requests.get(WISE_URL, params={'currentPage': page, 'endDT': end, 'fromDT': start, 'index_ids': ','.join(codes),
                                                       'isEnd': 1, 'itemType': 1, 'perPage': per, 'term': 1},
                                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=30, allow_redirects=False)
                    if r.status_code != 200:
                        raise RuntimeError(f'wiseindex 응답 {r.status_code}')
                    part = r.json()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(3)
            rows += part
            total = part[0]['TOT_ROW'] if part else 0
            if not part or page * per >= total:
                break
            page += 1
            time.sleep(0.3)
        data = {}
        for x in rows:
            d = (datetime.fromtimestamp(int(x['TRD_DT'][6:-2]) / 1000, timezone.utc) + timedelta(hours=9)).date()
            data[pd.Timestamp(d)] = [x.get(f'IDX{k + 1}_VAL1') for k in range(len(codes))]
        frames.append(pd.DataFrame.from_dict(data, orient='index', columns=[n for _, n in group]))
    df = pd.concat(frames, axis=1).sort_index()
    return df.apply(pd.to_numeric, errors='coerce')


def fetch_prices(start=DATA_START, now_utc=None):
    """업종지수 26개 + 코스피 → DataFrame(날짜 × [업종 26 … , '코스피']). 코스피는 야후, 빈 날은 KRX 지수(_krx_bench)."""
    import pandas as pd
    import yfinance as yf
    from collect_kr_etf_rs import _krx_bench

    sec = fetch_sectors(start)
    if sec.empty:
        raise RuntimeError('wiseindex 업종지수를 받지 못했습니다')
    k = yf.download('^KS11', start=(pd.Timestamp(start) - pd.Timedelta(days=10)).date().isoformat(), auto_adjust=False,
                    progress=False, threads=False)
    if k is None or k.empty:
        raise RuntimeError('야후 코스피를 받지 못했습니다')
    close = k['Close']
    close = close.iloc[:, 0] if hasattr(close, 'columns') else close
    close.index = pd.DatetimeIndex(close.index).tz_localize(None).normalize()
    px = sec.copy()
    px[BENCH] = close.reindex(px.index)
    kst = (now_utc or datetime.now(timezone.utc)) + timedelta(hours=9)
    px, nb = _krx_bench(px, kst)
    if nb:
        log.info(f'[국내월간] 야후에 없는 코스피 {nb}일을 KRX 지수로 채움')
    # 업종지수가 있는 날만 — 업종이 빈 줄이 끼면 120일선이 120일 동안 비어 버린다(코스피만 있는 오늘 줄 등)
    px = px.loc[px.index.isin(sec.index)]
    return px[px.index >= pd.Timestamp(start)]


def load_excel(path):
    """원본 엑셀(A열 날짜 / B열 코스피 / C열 이후 업종지수, '데이터' 시트) — 대조 시험용."""
    import pandas as pd

    book = pd.ExcelFile(path)
    sheet = '데이터' if '데이터' in book.sheet_names else book.sheet_names[0]
    raw = pd.read_excel(path, sheet_name=sheet).dropna(how='all')
    date_col, bench_col = raw.columns[0], raw.columns[1]
    raw[date_col] = pd.to_datetime(raw[date_col], errors='coerce')
    raw = raw.dropna(subset=[date_col]).sort_values(date_col).drop_duplicates(subset=[date_col], keep='last')
    df = raw.set_index(date_col)
    df = df.rename(columns={bench_col: BENCH})
    df.index.name = None
    return df.apply(pd.to_numeric, errors='coerce')


# ── 계산 (원본 2 ~ 12) ────────────────────────────────────────────────────

def compute(df) -> dict:
    """df: 날짜 × [업종 …, '코스피']. 원본 V2.3 코드와 같은 순서 · 같은 식으로 계산해 payload를 만든다."""
    import numpy as np
    import pandas as pd

    kospi = df[BENCH].copy()
    sector_cols = [c for c in df.columns if c != BENCH]
    sector_prices = df[sector_cols].copy()
    sector_cols = [c for c in sector_cols if sector_prices[c].notna().sum() >= 2]
    sector_prices = sector_prices[sector_cols]
    common = sector_prices.index.intersection(kospi.dropna().index)
    sector_prices = sector_prices.reindex(common)
    kospi = kospi.reindex(common)
    dates = sector_prices.index
    if len(sector_cols) < ENTRY_RANK or len(dates) < 600:
        raise RuntimeError(f'자료 부족 — 업종 {len(sector_cols)}개, {len(dates)}일')

    # 2. 기본 지표
    sector_returns = sector_prices.pct_change(fill_method=None)
    kospi_returns = kospi.pct_change(fill_method=None)
    ma120 = sector_prices.rolling(MA_DAYS, min_periods=MA_DAYS).mean()
    relative_strength = sector_prices.div(kospi, axis=0)
    relative_momentum = relative_strength / relative_strength.shift(MOMENTUM_DAYS) - 1
    sector_high_20 = sector_prices.rolling(RECOVERY_HIGH_DAYS, min_periods=RECOVERY_HIGH_DAYS).max()
    sector_dd_20 = sector_prices / sector_high_20 - 1
    kospi_high_252 = kospi.rolling(KOSPI_HIGH_DAYS, min_periods=120).max()
    kospi_dd_252 = kospi / kospi_high_252 - 1
    kospi_ma200 = kospi.rolling(KOSPI_TREND_MA_DAYS, min_periods=KOSPI_TREND_MA_DAYS).mean()
    kospi_abs_momentum_6m = kospi / kospi.shift(KOSPI_ABS_MOMENTUM_DAYS) - 1
    month_period = pd.Series(dates.to_period('M'), index=dates)
    month_end_mask = month_period != month_period.shift(-1)
    month_end_dates = dates[month_end_mask.fillna(True).values]

    # 3. 코스피 가격 기반 Fear & Greed
    def calculate_rsi(price, days=14):
        delta = price.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1 / days, adjust=False, min_periods=days).mean()
        avg_loss = loss.ewm(alpha=1 / days, adjust=False, min_periods=days).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi = rsi.where(avg_loss != 0.0, 100.0)
        rsi = rsi.where(avg_gain != 0.0, 0.0)
        return rsi.clip(0.0, 100.0)

    def rolling_percentile(series, window=252, min_periods=126):
        def percentile_of_last(values):
            values = np.asarray(values, dtype=float)
            current = values[-1]
            valid = values[np.isfinite(values)]
            if len(valid) == 0 or not np.isfinite(current):
                return np.nan
            less = np.sum(valid < current)
            equal = np.sum(valid == current)
            return 100.0 * (less + 0.5 * equal) / len(valid)
        return series.rolling(window, min_periods=min_periods).apply(percentile_of_last, raw=True)

    kospi_rsi = calculate_rsi(kospi, FG_RSI_DAYS)
    kospi_momentum_125 = kospi / kospi.shift(FG_MOMENTUM_DAYS) - 1
    kospi_ma20 = kospi.rolling(FG_MA_DAYS, min_periods=FG_MA_DAYS).mean()
    kospi_ma_distance = kospi / kospi_ma20 - 1
    kospi_high_fg = kospi.rolling(FG_HIGH_DAYS, min_periods=126).max()
    kospi_high_position = kospi / kospi_high_fg - 1
    kospi_volatility_20 = kospi_returns.rolling(FG_VOL_DAYS, min_periods=FG_VOL_DAYS).std() * np.sqrt(252)
    fear_greed_index = (
        0.25 * rolling_percentile(kospi_rsi, FG_PERCENTILE_DAYS)
        + 0.25 * rolling_percentile(kospi_momentum_125, FG_PERCENTILE_DAYS)
        + 0.20 * rolling_percentile(kospi_ma_distance, FG_PERCENTILE_DAYS)
        + 0.15 * rolling_percentile(kospi_high_position, FG_PERCENTILE_DAYS)
        + 0.15 * (100.0 - rolling_percentile(kospi_volatility_20, FG_PERCENTILE_DAYS))
    ).clip(0.0, 100.0)
    fg_fast = fear_greed_index.ewm(span=FG_FAST_EMA, adjust=False, min_periods=FG_FAST_EMA).mean()
    fg_slow = fear_greed_index.ewm(span=FG_SLOW_EMA, adjust=False, min_periods=FG_SLOW_EMA).mean()
    fg_oscillator = fg_fast - fg_slow
    fg_signal = fg_oscillator.ewm(span=FG_SIGNAL_EMA, adjust=False, min_periods=FG_SIGNAL_EMA).mean()
    fg_histogram = fg_oscillator - fg_signal

    # 4. 보조 함수
    def get_overlay_exposure(drawdown):
        if pd.isna(drawdown):
            return 0.0
        for threshold, exposure in FINAL_OVERLAY_LEVELS:
            if drawdown <= threshold:
                return exposure
        return 0.0

    def increase_exposure_one_step(exposure):
        exposure = float(np.clip(exposure, 0.0, 1.0))
        for step in EXPOSURE_STEPS:
            if step > exposure + 1e-12:
                return step
        return 1.0

    def get_non_trend_exposure(avg_rs):
        if pd.isna(avg_rs):
            return 0.0
        if avg_rs >= RS_CUT_3:
            return NON_TREND_EXPOSURES['HIGH']
        if avg_rs >= RS_CUT_2:
            return NON_TREND_EXPOSURES['MID_HIGH']
        if avg_rs >= RS_CUT_1:
            return NON_TREND_EXPOSURES['MID_LOW']
        return NON_TREND_EXPOSURES['LOW']

    def adjust_non_trend_exposure_v22(signal_date, base_exposure):
        ma200_value = kospi_ma200.loc[signal_date]
        momentum_6m = kospi_abs_momentum_6m.loc[signal_date]
        drawdown = kospi_dd_252.loc[signal_date]
        kospi_price = kospi.loc[signal_date]
        adjusted = float(base_exposure)
        regime = 'NEUTRAL_NON_TREND'
        if not (pd.notna(ma200_value) and pd.notna(momentum_6m) and pd.notna(drawdown) and pd.notna(kospi_price)):
            return float(np.clip(adjusted, 0.0, 1.0)), regime
        if (kospi_price < ma200_value and momentum_6m < NON_TREND_WEAK_MOMENTUM_THRESHOLD
                and drawdown <= NON_TREND_WEAK_DD_THRESHOLD):
            adjusted -= NON_TREND_WEAK_CUT
            regime = 'WEAK_NON_TREND'
        return float(np.clip(adjusted, 0.0, 1.0)), regime

    def rank_eligible_sectors(signal_date):
        price_now = sector_prices.loc[signal_date]
        ma_now = ma120.loc[signal_date]
        relmom_now = relative_momentum.loc[signal_date]
        eligible = (price_now.notna() & ma_now.notna() & relmom_now.notna() & (price_now > ma_now) & (relmom_now > 0))
        return relmom_now[eligible].sort_values(ascending=False)

    def select_with_buffer(ranked, previous_holdings):
        ranking = {sector: rank for rank, sector in enumerate(ranked.index, start=1)}
        selected = [s for s in previous_holdings if s in ranking and ranking[s] <= HOLD_RANK]
        for sector in ranked.index:
            if ranking[sector] > ENTRY_RANK:
                break
            if sector not in selected:
                selected.append(sector)
            if len(selected) >= ENTRY_RANK:
                break
        if len(selected) > ENTRY_RANK:
            selected = sorted(selected, key=lambda x: ranking.get(x, 999))[:ENTRY_RANK]
        return selected

    def select_recovery_pullbacks(signal_date, ranked, pool):
        if len(ranked) == 0 or not pool:
            return []
        current_dd = sector_dd_20.loc[signal_date]
        cands = [s for s in ranked.index if s in pool and pd.notna(current_dd.get(s, np.nan))
                 and RECOVERY_DD_MIN <= current_dd[s] <= RECOVERY_DD_MAX]
        return cands[:ENTRY_RANK]

    def previous_month_end_date(signal_date):
        position = month_end_dates.get_indexer([signal_date])[0]
        if position <= 0:
            return None
        return month_end_dates[position - 1]

    def get_fg_oversold_rebound_signal(signal_date):
        previous_date = previous_month_end_date(signal_date)
        if previous_date is None:
            return False, np.nan, np.nan, np.nan
        fg_value = fear_greed_index.loc[signal_date]
        hist_now = fg_histogram.loc[signal_date]
        hist_previous = fg_histogram.loc[previous_date]
        valid = pd.notna(fg_value) and pd.notna(hist_now) and pd.notna(hist_previous)
        signal = valid and fg_value <= FG_OVERSOLD_LEVEL and hist_now > 0 and hist_now > hist_previous
        return bool(signal), fg_value, hist_now, hist_previous

    # 5. 월말 목표 포트폴리오
    warmup = max(MOMENTUM_DAYS, MA_DAYS, KOSPI_HIGH_DAYS, RECOVERY_HIGH_DAYS, FG_HIGH_DAYS + FG_PERCENTILE_DAYS)

    def build_targets(strategy_type):
        current_holdings = []
        top3_history = deque(maxlen=RECOVERY_HISTORY_MONTHS)
        rows, targets = [], {}
        for signal_date in month_end_dates:
            i = dates.get_loc(signal_date)
            if i < warmup:
                continue
            price_now = sector_prices.loc[signal_date]
            ma_now = ma120.loc[signal_date]
            valid_breadth = price_now.notna() & ma_now.notna()
            if valid_breadth.sum() == 0:
                continue
            breadth = (price_now[valid_breadth] > ma_now[valid_breadth]).mean()
            ranked = rank_eligible_sectors(signal_date)
            current_top3_set = set(ranked.head(ENTRY_RANK).index)
            pool = set().union(*top3_history) if top3_history else set()
            kospi_dd = kospi_dd_252.loc[signal_date]
            previous_holdings = current_holdings.copy()
            sector_target = pd.Series(0.0, index=sector_cols, dtype=float)
            kospi_target = base_kospi_target = final_exposure = 0.0
            top3_average_rs = np.nan
            recovery = []
            fg_boost = False
            nt_base = np.nan
            nt_regime = ''
            fg_value = fear_greed_index.loc[signal_date]
            fg_osc_value = fg_oscillator.loc[signal_date]
            fg_signal_value = fg_signal.loc[signal_date]
            fg_hist_value = fg_histogram.loc[signal_date]
            previous_fg_hist = np.nan
            mode = 'CASH'
            if breadth >= BREADTH_TREND:                         # A. 추세
                current_holdings = select_with_buffer(ranked, previous_holdings)
                if current_holdings:
                    final_exposure = 1.0
                    sector_target.loc[current_holdings] = final_exposure / len(current_holdings)
                mode = 'TREND'
            elif breadth >= BREADTH_NON_TREND:                   # B. 비추세
                top3_scores = ranked.head(ENTRY_RANK)
                if len(top3_scores) > 0:
                    top3_average_rs = top3_scores.mean()
                nt_base = get_non_trend_exposure(top3_average_rs)
                final_exposure = nt_base
                if strategy_type == 'final_v2_2':
                    final_exposure, nt_regime = adjust_non_trend_exposure_v22(signal_date, nt_base)
                current_holdings = select_with_buffer(ranked, previous_holdings)
                if current_holdings and final_exposure > 0:
                    sector_target.loc[current_holdings] = final_exposure / len(current_holdings)
                else:
                    final_exposure = 0.0
                    current_holdings = []
                mode = f'NON_TREND_V22_{nt_regime}' if strategy_type == 'final_v2_2' else 'NON_TREND'
            elif breadth >= BREADTH_CONTRARIAN:                  # C. 회복
                recovery = select_recovery_pullbacks(signal_date, ranked, pool)
                current_holdings = recovery.copy()
                if current_holdings:
                    final_exposure = RECOVERY_EXPOSURE
                    sector_target.loc[current_holdings] = final_exposure / len(current_holdings)
                    mode = 'RECOVERY_LEADER_PULLBACK'
                else:
                    current_holdings = []
                    final_exposure = 0.0
                    mode = 'RECOVERY_CASH'
            else:                                                # D. 역추세
                current_holdings = []
                base_kospi_target = get_overlay_exposure(kospi_dd)
                kospi_target = base_kospi_target
                if strategy_type in ('final_v2_1', 'final_v2_2'):
                    fg_boost, fg_value, fg_hist_value, previous_fg_hist = get_fg_oversold_rebound_signal(signal_date)
                    if fg_boost:
                        kospi_target = increase_exposure_one_step(base_kospi_target)
                final_exposure = kospi_target
                if kospi_target > 0:
                    mode = 'CONTRARIAN_KOSPI_FG_BOOST' if kospi_target > base_kospi_target else 'CONTRARIAN_KOSPI'
                else:
                    mode = 'CASH'
            if i + 1 < len(dates):
                effective_date = dates[i + 1]
                targets[effective_date] = {'sector_weights': sector_target.copy(), 'kospi_weight': float(kospi_target)}
            else:
                effective_date = None
            rows.append({
                'sd': signal_date, 'ed': effective_date, 'mode': mode, 'breadth': float(breadth), 'dd': kospi_dd,
                'base': base_kospi_target, 'exp': final_exposure, 'cash': max(0.0, 1.0 - final_exposure),
                'fg': fg_value, 'osc': fg_osc_value, 'sig': fg_signal_value, 'hist': fg_hist_value, 'prev_hist': previous_fg_hist,
                'boost': bool(fg_boost), 'top3_rs': top3_average_rs, 'nt_base': nt_base, 'nt_regime': nt_regime,
                'ma200': kospi_ma200.loc[signal_date], 'mom6': kospi_abs_momentum_6m.loc[signal_date],
                'prev': list(previous_holdings), 'hold': ['KOSPI'] if kospi_target > 0 else list(current_holdings),
                'rec': list(recovery), 'pool': sorted(pool), 'top3': list(ranked.head(ENTRY_RANK).index),
                'top5': list(ranked.head(HOLD_RANK).index),
                'top5_rs': [float(v) for v in ranked.head(HOLD_RANK).values],
            })
            top3_history.append(current_top3_set)
        return rows, targets

    # 6. 백테스트 엔진
    R = sector_returns.reindex(columns=sector_cols).fillna(0.0).to_numpy()
    KR = kospi_returns.fillna(0.0).to_numpy()

    def run_backtest(strategy_type):
        rows, targets = build_targets(strategy_type)
        n = len(dates)
        equity = np.ones(n)
        turnover = np.zeros(n)
        w = np.zeros(len(sector_cols))
        kw = 0.0
        rebalance_count = 0
        for i in range(1, n):
            date_i = dates[i]
            prev_eq = equity[i - 1]
            t = targets.get(date_i)
            if t is not None:
                tw = t['sector_weights'].reindex(sector_cols).fillna(0.0).to_numpy()
                tk = t['kospi_weight']
                to = float(np.abs(tw - w).sum() + abs(tk - kw))
                turnover[i] = to
                if to > 1e-12:
                    rebalance_count += 1
                prev_eq *= max(0.0, 1 - TRANSACTION_COST * to)
                w = tw.copy()
                kw = float(tk)
            r = R[i]
            kr = KR[i]
            cash = max(0.0, 1.0 - w.sum() - kw)
            mult = cash + float((w * (1 + r)).sum()) + kw * (1 + kr)
            equity[i] = prev_eq * mult
            if mult > 0:
                w = w * (1 + r) / mult
                kw = kw * (1 + kr) / mult
        eq = pd.Series(equity, index=dates)
        active = sorted(x['ed'] for x in rows if x['exp'] > 0 and x['ed'] is not None)
        live_start = pd.Timestamp(active[0]) if active else eq.index[0]
        loc = eq.index.get_loc(live_start)
        base = eq.iloc[loc - 1] if loc > 0 else eq.iloc[loc]
        live = eq.loc[live_start:] / base
        data_years = (eq.index[-1] - eq.index[0]).days / 365.25
        live_years = (live.index[-1] - live.index[0]).days / 365.25
        dret = live.pct_change().dropna()
        sd = dret.std()
        dsd = dret[dret < 0].std()
        monthly = live.resample('ME').last().pct_change().dropna()
        to_ser = pd.Series(turnover, index=dates)
        return {
            'rows': rows, 'equity': eq, 'live_start': live_start,
            'perf': {
                'data_start': eq.index[0], 'live_start': live_start,
                'data_cagr': (eq.iloc[-1] / eq.iloc[0]) ** (1 / data_years) - 1,
                'live_cagr': (live.iloc[-1] / live.iloc[0]) ** (1 / live_years) - 1 if live_years > 0 else np.nan,
                'mdd': (live / live.cummax() - 1).min(),
                'sharpe': dret.mean() / sd * np.sqrt(252) if sd > 0 else np.nan,
                'sortino': dret.mean() / dsd * np.sqrt(252) if pd.notna(dsd) and dsd > 0 else np.nan,
                'vol': sd * np.sqrt(252),
                'win': (monthly > 0).mean() if len(monthly) else np.nan,
                'mult': live.iloc[-1],
                'turnover': to_ser.loc[live_start:].sum() / live_years if live_years > 0 else np.nan,
                'rebal': rebalance_count,
            },
        }

    res = {k: run_backtest(st) for k, st in STRATS}

    # 8. 코스피 성과
    kospi_equity = (1 + kospi_returns.fillna(0.0)).cumprod()
    kospi_equity = kospi_equity / kospi_equity.iloc[0]
    k_years = (kospi_equity.index[-1] - kospi_equity.index[0]).days / 365.25
    k_ret = kospi_equity.pct_change().dropna()
    k_sd = k_ret.std()
    k_monthly = kospi_equity.resample('ME').last().pct_change().dropna()
    kospi_perf = {
        'data_start': kospi_equity.index[0], 'live_start': kospi_equity.index[0],
        'data_cagr': (kospi_equity.iloc[-1] / kospi_equity.iloc[0]) ** (1 / k_years) - 1,
        'live_cagr': (kospi_equity.iloc[-1] / kospi_equity.iloc[0]) ** (1 / k_years) - 1,
        'mdd': (kospi_equity / kospi_equity.cummax() - 1).min(),
        'sharpe': k_ret.mean() / k_sd * np.sqrt(252) if k_sd > 0 else np.nan,
        'sortino': np.nan, 'vol': k_sd * np.sqrt(252), 'win': (k_monthly > 0).mean(),
        'mult': kospi_equity.iloc[-1], 'turnover': 0.0, 'rebal': 0,
    }

    # 10. 연도별 수익률
    def yearly(curve, start):
        out = {}
        part = curve.loc[start:]
        for y in sorted(part.index.year.unique()):
            yc = part[part.index.year == y]
            if len(yc) == 0:
                continue
            loc = curve.index.get_loc(yc.index[0])
            sv = curve.iloc[loc - 1] if loc > 0 else yc.iloc[0]
            out[int(y)] = yc.iloc[-1] / sv - 1
        return out
    cmp_start = min(res['v2']['live_start'], res['v21']['live_start'])
    yr = {k: yearly(res[k]['equity'], res[k]['live_start']) for k, _ in STRATS}
    yr['kospi'] = yearly(kospi_equity, cmp_start)
    years = sorted(set().union(*[set(v) for v in yr.values()]))

    # 11. F&G 과매도 확대 발동 이력(V2.1)
    boost = [r for r in res['v21']['rows'] if r['mode'] == 'CONTRARIAN_KOSPI_FG_BOOST']

    # 12-1. 이번 달 확정 / 다음 달 예상(V2.3)
    rows23 = res['v23']['rows']
    latest = dates[-1]
    conf = [r for r in rows23 if r['ed'] is not None and r['ed'] <= latest]
    confirmed = sorted(conf, key=lambda r: (r['ed'], r['sd']))[-1] if conf else rows23[-1]
    prev_c = [r for r in rows23 if r['ed'] is None and r['sd'] <= latest]
    preview = prev_c[-1] if prev_c else rows23[-1]

    # 13. 그림 — V2와 V2.1 중 늦게 시작한 날부터(원본과 같음), 그날 = 1
    graph_start = max(res['v2']['live_start'], res['v21']['live_start'])
    def norm(curve):
        c = curve.loc[graph_start:]
        return c / c.iloc[0]
    curves = {k: norm(res[k]['equity']) for k, _ in STRATS}
    curves['kospi'] = norm(kospi_equity)
    gdates = curves['v23'].index

    # 지금 업종 현황(원본 화면엔 없음 — Breadth · 상위 업종이 왜 그렇게 나왔는지 보이려고)
    last = dates[-1]
    ranked_now = rank_eligible_sectors(last)
    rank_map = {s: i for i, s in enumerate(ranked_now.index, start=1)}
    now = []
    for s in sector_cols:
        p, m = sector_prices.loc[last, s], ma120.loc[last, s]
        now.append({'n': s, 'code': dict((n, c) for c, n in SECTORS).get(s), 'close': _num(p, 2),
                    'ma_gap': _num((p / m - 1) if pd.notna(p) and pd.notna(m) and m else None, 4),
                    'rs': _num(relative_momentum.loc[last, s], 4), 'dd20': _num(sector_dd_20.loc[last, s], 4),
                    'r1m': _num(sector_prices[s].iloc[-1] / sector_prices[s].iloc[-22] - 1 if len(dates) > 22 else None, 4),
                    'elig': s in rank_map, 'rank': rank_map.get(s)})

    def sig(r):
        return {
            'sd': r['sd'].date().isoformat(), 'ed': r['ed'].date().isoformat() if r['ed'] is not None else None,
            'mode': r['mode'], 'breadth': _num(r['breadth'], 4), 'dd': _num(r['dd'], 4), 'base': _num(r['base'], 2),
            'exp': _num(r['exp'], 4), 'cash': _num(r['cash'], 4), 'fg': _num(r['fg'], 2), 'osc': _num(r['osc'], 4),
            'sig': _num(r['sig'], 4), 'hist': _num(r['hist'], 4), 'prev_hist': _num(r['prev_hist'], 4), 'boost': r['boost'],
            'top3_rs': _num(r['top3_rs'], 4), 'nt_base': _num(r['nt_base'], 2), 'nt_regime': r['nt_regime'],
            'ma200': _num(r['ma200'], 2), 'mom6': _num(r['mom6'], 4), 'prev': r['prev'], 'hold': r['hold'], 'rec': r['rec'],
            'pool': r['pool'], 'top3': r['top3'], 'top5': r['top5'], 'top5_rs': [_num(v, 4) for v in r['top5_rs']],
        }

    def perf(p):
        return {'data_start': p['data_start'].date().isoformat(), 'live_start': p['live_start'].date().isoformat(),
                'data_cagr': _num(p['data_cagr'], 4), 'live_cagr': _num(p['live_cagr'], 4), 'mdd': _num(p['mdd'], 4),
                'sharpe': _num(p['sharpe'], 3), 'sortino': _num(p['sortino'], 3), 'vol': _num(p['vol'], 4), 'win': _num(p['win'], 4),
                'mult': _num(p['mult'], 4), 'turnover': _num(p['turnover'], 2), 'rebal': int(p['rebal'])}

    return {
        'asof': last.date().isoformat(), 'start': dates[0].date().isoformat(), 'n': len(sector_cols),
        'sectors': [{'c': c, 'n': n} for c, n in SECTORS if n in sector_cols], 'src': 'wiseindex',
        'confirmed': sig(confirmed), 'preview': sig(preview),
        'signals': [sig(r) for r in rows23[-RECENT_SIGNALS:]],
        'perf': {**{k: perf(res[k]['perf']) for k, _ in STRATS}, 'kospi': perf(kospi_perf)},
        'yearly': {'years': years, **{k: [_num(yr[k].get(y), 4) for y in years] for k in ('v2', 'v21', 'v23', 'kospi')}},
        'boost': [sig(r) for r in boost],
        'graph_start': graph_start.date().isoformat(),
        'curve': {'dates': [d.date().isoformat() for d in gdates], **{k: [_num(v, 4) for v in curves[k].reindex(gdates)] for k in curves}},
        'fg': {'v': [_num(v, 2) for v in fear_greed_index.reindex(gdates)], 'hist': [_num(v, 3) for v in fg_histogram.reindex(gdates)]},
        'now': sorted(now, key=lambda x: (x['rank'] is None, x['rank'] or 0, -(x['rs'] if x['rs'] is not None else -9))),
        'kospi_now': {'close': _num(kospi.iloc[-1], 2), 'dd': _num(kospi_dd_252.iloc[-1], 4), 'ma200': _num(kospi_ma200.iloc[-1], 2),
                      'mom6': _num(kospi_abs_momentum_6m.iloc[-1], 4), 'fg': _num(fear_greed_index.iloc[-1], 2),
                      'hist': _num(fg_histogram.iloc[-1], 4)},
    }


def run(dry: bool = False, excel: str = None) -> dict:
    sb = None if dry else get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[국내월간] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    df = load_excel(excel) if excel else fetch_prices()
    p = compute(df)
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': p['asof'], 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
        cutoff = (date.fromisoformat(p['asof']) - timedelta(days=KEEP_DAYS)).isoformat()
        sb.table(TABLE).delete().eq('kind', KIND).lt('base_date', cutoff).execute()
    c, v = p['confirmed'], p['preview']
    log.info(f"[국내월간] {'dry-run ' if dry else ''}{p['asof']} 업종 {p['n']} · 확정 {c['sd']} {c['mode']} {c['exp']} {','.join(c['hold']) or '-'}"
             f" · 예상 {v['mode']} {v['exp']} {','.join(v['hold']) or '-'} · Breadth {v['breadth']} · V2.3 {p['perf']['v23']['mult']}배")
    return {'date': p['asof'], 'payload': p}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    ap.add_argument('--dump', help='계산 결과를 이 경로에 JSON으로 저장')
    ap.add_argument('--excel', help='원본 엑셀(날짜·코스피·업종지수)로 계산 — 대조 시험용')
    a = ap.parse_args()
    r = run(dry=a.dry or bool(a.excel), excel=a.excel)
    if a.dump and r.get('payload'):
        with open(a.dump, 'w', encoding='utf-8') as f:
            json.dump(r['payload'], f, ensure_ascii=False)
    print({k: v for k, v in r.items() if k != 'payload'})
