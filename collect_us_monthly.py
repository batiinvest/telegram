"""
collect_us_monthly.py — 미국 월 리밸런싱 V8.2 · 개선 2조건(RESEARCH_TWO, ETF 52개) → market_snapshot(kind='us_monthly')

[원본] 태린이아빠 Market Dashboard '미국 월단위 리밸런싱 투자전략'(공개 소스 app.py US_MONTHLY_REBALANCE_SRC, v11.67 · 11-22판)
  매달 마지막 거래일 종가로 업종 ETF의 120일선 위 비율(Breadth)과 NASDAQ100 · S&P500 상대강도를 보고 다음 달 자산을 정한다.
  · 나스닥 독주(QQQ > 200일선 ∧ QQQ/SPY 6개월 RS > +3.5%): 나스닥 세부(KRE · KBWB · IYF · IBB · IYW) 중 QQQ 대비 6개월 RS 1위 100%
      (Breadth < 30%면 50%)
  · 초광범위 강세(Breadth ≥ 70%): SPY와 QQQ 중 1 · 3 · 6개월 절대수익 평균이 높은 쪽 100%
  · 선택적 강세(60 ~ 70%): 적격 업종(종가 > 120일선 ∧ 6개월 SPY 대비 RS > 0) 중 3개월 SPY 대비 RS 상위 2 균등
  · 비추세(40 ~ 60%): 적격 업종 중 '최근 1개월 뺀 6개월 일별 횡단면 순위 평균' 상위 3, 비중은 그 3개의 6개월 RS 평균
      <5% · 5~12% · 12~22% · ≥22% → 25 · 50 · 75 · 100%(개선안 최대 25%). 25% 이하면 금속 1위 50% 추가(최대 75%)
  · 회복(30 ~ 40%): 최근 6개 월말 6개월 RS 상위 3에 들었던 업종 중 20일 고점 대비 −15 ~ −5% 눌린 것 최대 3개에 50%, 없으면 금속 50%
  · 깊은 약세(< 30%): SPY 252일 고점 대비 −12 · −18 · −24% 이하 → 50 · 75 · 100% SPY, 0%면 금속 50%
  · 금속(XME · PICK · SIL): 120일선 위 ∧ 6개월 SPY 대비 RS > 0 중 1위
  · 백테스트: 준비 200거래일 뒤 첫 월말부터 신호 → 다음 거래일 종가 체결 → 그다음 날부터 수익, 편도 0.1%, 비중은 매일 가격대로.
      성과는 첫 투자 체결 전날부터 전체 기간 SPY · QQQ와 비교(V8.1의 '최근 2년'은 원본에서 폐지).
  · '이번 달 확정' = 직전 월말 신호, '다음 달 예상' = 최신 거래일을 월말로 친 신호(아직 적용 전).
  · 가격 점검: 하루 +200% 초과 · −70% 미만 · 0 이하 가격이 하나라도 있으면 계산을 멈춘다(V8.2 — 예전처럼 그 ETF를 빼지 않는다).
[자료] 야후 수정 종가(auto_adjust=True — 분할 · 배당 반영) 52개, 원본과 같은 2018-12-07부터. 원본 HTS 종가도 배당 반영(SPY 2025 +17.72% 일치).
  원본은 상대강도 PDF 전용으로 XLV · XLY · XLC · IWM · RSP 5개를 더 읽지만 매매 · Breadth에 안 쓰고 대시보드에도 안 나와 받지 않는다.
[돌리는 때] 매일 아침(미국 장 마감 뒤) 두 번 — 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다.
"""
import argparse
import json
import logging
import math
from collections import deque
from datetime import date, datetime, timedelta, timezone

from db_client import get_supabase_client

log = logging.getLogger(__name__)

TABLE = 'market_snapshot'
KIND = 'us_monthly'
KEEP_DAYS = 10
DATA_START = '2018-12-07'      # 원본 HTS 엑셀 시작일(v11.65부터 — 그전 판은 2020-10-27)
VERSION = 'V8.2'

# ── 원본 0. 설정 (V8.2 · RESEARCH_TWO) ──────────────────────────────────
TRANSACTION_COST = 0.001
MOMENTUM_1M, MOMENTUM_3M, MOMENTUM_6M = 21, 63, 126
NON_TREND_RANK_LOOKBACK = MOMENTUM_6M - MOMENTUM_1M
SECTOR_MA_DAYS = 120
NASDAQ_MA_DAYS = 200
BREADTH_TREND = 0.60
BREADTH_NON_TREND = 0.40
BREADTH_RECOVERY = 0.30
ATTACK_TOP_N = 2
NON_TREND_TOP_N = 3
RECOVERY_HISTORY_MONTHS = 6
RECOVERY_EXPOSURE = 0.50
RECOVERY_HIGH_DAYS = 20
RECOVERY_DD_MIN, RECOVERY_DD_MAX = -0.15, -0.05
REAL_ASSET_RS_THRESHOLD = 0.00
PARAM_SET = 'RESEARCH_TWO'     # 원본 개선안 기본값(기존 V8.2 비교는 'SUPER70')
TUNING_PROFILES = {
    'ORIGINAL':     {'superbull': .80, 'nasdaq_rs': .05,  'nontrend_max': .50, 'metal': .50},
    'SUPER70':      {'superbull': .70, 'nasdaq_rs': .05,  'nontrend_max': .50, 'metal': .50},
    'BALANCED':     {'superbull': .80, 'nasdaq_rs': .035, 'nontrend_max': .25, 'metal': .75},
    'RESEARCH70':   {'superbull': .70, 'nasdaq_rs': .035, 'nontrend_max': .25, 'metal': .75},
    'RESEARCH_TWO': {'superbull': .70, 'nasdaq_rs': .035, 'nontrend_max': .25, 'metal': .50},
}
_T = TUNING_PROFILES[PARAM_SET]
BREADTH_SUPER_BULL = _T['superbull']
NASDAQ_RS_THRESHOLD = _T['nasdaq_rs']
NON_TREND_MAX_EXPOSURE = _T['nontrend_max']
REAL_ASSET_EXPOSURE = _T['metal']
SP500_DD_LEVELS = [(-0.24, 1.00), (-0.18, 0.75), (-0.12, 0.50)]
SP500_NAME, NASDAQ100_NAME = 'SPY', 'QQQ'
NASDAQ_INTERNAL_NAMES = ['KRE', 'KBWB', 'IYF', 'IBB', 'IYW']
REAL_ASSET_NAMES = ['XME', 'PICK', 'SIL']
BASE_ETFS = ['SPY', 'SOXX', 'IGV', 'XME', 'FDN', 'GDX', 'IYZ', 'FINX', 'KCE', 'XLE', 'KIE', 'XLI', 'IHI', 'IHF', 'IYT', 'ITA', 'XLB',
             'BOAT', 'PICK', 'PPH', 'PPA', 'OIH', 'PEJ', 'PHO', 'XHB', 'IYR', 'PBJ', 'XLP', 'CARZ', 'QQQ', 'KRE', 'KBWB', 'IYF', 'IBB',
             'IYW', 'SIL']
ADDED_ETFS = ['FCG', 'XOP', 'MLPX', 'XLU', 'URA', 'GRID', 'TAN', 'COPX', 'SLX', 'REMX', 'MOO', 'WOOD', 'AIRR', 'XBI', 'SKYY', 'LIT']
ETFS = BASE_ETFS + ADDED_ETFS
SECTOR_ETFS = ['SOXX', 'IGV', 'FDN', 'GDX', 'IYZ', 'FINX', 'KCE', 'XLE', 'KIE', 'XLI', 'IHI', 'IHF', 'IYT', 'ITA', 'XLB', 'BOAT', 'PPH',
               'PPA', 'OIH', 'PEJ', 'PHO', 'XHB', 'IYR', 'PBJ', 'XLP', 'CARZ'] + ADDED_ETFS
MIN_HISTORY_ROWS = 127
WARMUP = 200                   # V8.2: 504 → 200거래일
MAX_DAILY_GAIN, MAX_DAILY_LOSS = 2.00, -0.70
RECENT_SIGNALS = 15


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


def fetch_prices(start=DATA_START, now_utc=None):
    """야후 수정 종가(분할 · 배당 반영) 52개 → DataFrame(날짜 × 티커, ETFS 순서). 미국 장중이면 오늘 봉은 버린다."""
    import pandas as pd
    import yfinance as yf

    raw = yf.download(ETFS, start=start, auto_adjust=True, actions=False, progress=False, threads=False, group_by='column')
    if raw is None or raw.empty:
        raise RuntimeError('야후 일봉을 받지 못했습니다')
    px = raw['Close'].copy()
    px.index = pd.DatetimeIndex(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    now = now_utc or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo('America/New_York'))
    except Exception:            # tzdata 없는 환경(윈도 시험) — 동부 표준시로 근사
        et = (now - timedelta(hours=5)).replace(tzinfo=None)
    if len(px) and px.index[-1].date() == et.date() and (et.hour, et.minute) < (16, 15):
        px = px.iloc[:-1]
    cols = [c for c in ETFS if c in px.columns]
    return px[cols]


def compute(raw) -> dict:
    """raw: 날짜 × 티커 종가. 원본 V8.2 코드와 같은 순서 · 같은 식."""
    import numpy as np
    import pandas as pd

    # 1 · 2. 열 · 날짜 정리(원본: 3거래일까지 앞 값으로 채우고, 유효 종가 127일 미만 열은 뺀다)
    data = raw[[c for c in ETFS if c in raw.columns]].copy().sort_index()
    data = data[~data.index.duplicated(keep='last')]
    data = data.ffill(limit=3)
    data = data.loc[:, data.notna().sum() >= MIN_HISTORY_ROWS]
    for req in (SP500_NAME, NASDAQ100_NAME):
        if req not in data.columns:
            raise RuntimeError(f'필수 지수 없음: {req}')
    nas_names = [x for x in NASDAQ_INTERNAL_NAMES if x in data.columns]
    real_names = [x for x in REAL_ASSET_NAMES if x in data.columns]
    if not nas_names or not real_names:
        raise RuntimeError('NASDAQ 내부지수 · 금속이 없습니다')
    # 4. 가격 점검(V8.2) — 모든 ETF에서 0 이하 · 하루 +200% 초과 · −70% 미만이 있으면 빼지 않고 멈춘다
    events = []
    for c in data.columns:
        s = data[c]
        bad_px = s.notna() & ((s <= 0) | ~np.isfinite(s))
        events += [f'{c} {d.date()} 가격 {s.loc[d]}' for d in s.index[bad_px]]
        r = s.pct_change(fill_method=None)
        events += [f'{c} {d.date()} {ch:+.1%}' for d, ch in r[(r > MAX_DAILY_GAIN) | (r < MAX_DAILY_LOSS)].items()]
    if events:
        raise RuntimeError(f'ETF 가격 이상 {len(events)}건 — 분할 · 수정종가 확인 필요: ' + '; '.join(events[:10]))
    # 일반 업종 — 금속은 따로 쓰므로 뺀다
    sector_cols = [c for c in data.columns if c in SECTOR_ETFS and c not in real_names]
    if len(sector_cols) < ATTACK_TOP_N:
        raise RuntimeError('일반 업종 수가 부족합니다')

    # 5. 기본 데이터
    dates = data.index
    sp500, nasdaq100 = data[SP500_NAME].copy(), data[NASDAQ100_NAME].copy()
    sector_prices = data[sector_cols].copy()
    nasdaq_internal = data[nas_names].copy()
    real_assets = data[real_names].copy()
    sector_returns = sector_prices.pct_change(fill_method=None)
    nasdaq_internal_returns = nasdaq_internal.pct_change(fill_method=None)
    real_asset_returns = real_assets.pct_change(fill_method=None)
    sp500_returns = sp500.pct_change(fill_method=None)
    nasdaq100_returns = nasdaq100.pct_change(fill_method=None)

    # 6. 지표
    sector_ma120 = sector_prices.rolling(SECTOR_MA_DAYS, min_periods=SECTOR_MA_DAYS).mean()
    sector_relative = sector_prices.div(sp500, axis=0)
    sector_rs_6m = sector_relative / sector_relative.shift(MOMENTUM_6M) - 1
    sector_rs_3m = sector_relative / sector_relative.shift(MOMENTUM_3M) - 1
    sector_daily_rank = sector_returns.rank(axis=1, pct=True, method='average')
    sector_rank_mom = sector_daily_rank.shift(MOMENTUM_1M).rolling(NON_TREND_RANK_LOOKBACK, min_periods=NON_TREND_RANK_LOOKBACK).mean()
    sector_high20 = sector_prices.rolling(RECOVERY_HIGH_DAYS, min_periods=RECOVERY_HIGH_DAYS).max()
    sector_dd20 = sector_prices / sector_high20 - 1
    nasdaq_ma200 = nasdaq100.rolling(NASDAQ_MA_DAYS, min_periods=NASDAQ_MA_DAYS).mean()
    nasdaq_vs_sp = nasdaq100 / sp500
    nasdaq_vs_sp_rs_6m = nasdaq_vs_sp / nasdaq_vs_sp.shift(MOMENTUM_6M) - 1
    nasdaq_internal_rs_6m = nasdaq_internal.div(nasdaq100, axis=0)
    nasdaq_internal_rs_6m = nasdaq_internal_rs_6m / nasdaq_internal_rs_6m.shift(MOMENTUM_6M) - 1
    mom = lambda s, n: s / s.shift(n) - 1
    sp500_mom_1_3_6 = (mom(sp500, MOMENTUM_1M) + mom(sp500, MOMENTUM_3M) + mom(sp500, MOMENTUM_6M)) / 3.0
    nasdaq100_mom_1_3_6 = (mom(nasdaq100, MOMENTUM_1M) + mom(nasdaq100, MOMENTUM_3M) + mom(nasdaq100, MOMENTUM_6M)) / 3.0
    real_asset_rs_6m = real_assets.div(sp500, axis=0)
    real_asset_rs_6m = real_asset_rs_6m / real_asset_rs_6m.shift(MOMENTUM_6M) - 1
    real_asset_ma120 = real_assets.rolling(SECTOR_MA_DAYS, min_periods=SECTOR_MA_DAYS).mean()
    sp500_dd252 = sp500 / sp500.rolling(252, min_periods=120).max() - 1

    # 7. 월말
    month_period = pd.Series(dates.to_period('M'), index=dates)
    month_end_dates = dates[(month_period != month_period.shift(-1)).fillna(True).values]

    # 8. 보조 함수
    def contrarian_exposure(dd):
        if pd.isna(dd):
            return 0.0
        for th, ex in SP500_DD_LEVELS:
            if dd <= th:
                return ex
        return 0.0

    def eligible_mask(d):
        price, ma, rs6 = sector_prices.loc[d], sector_ma120.loc[d], sector_rs_6m.loc[d]
        return price.notna() & ma.notna() & rs6.notna() & (price > ma) & (rs6 > 0)

    def rank_sector_6m(d):
        rs = sector_rs_6m.loc[d]
        return rs[eligible_mask(d)].sort_values(ascending=False)

    def rank_sector_3m(d):
        rs3 = sector_rs_3m.loc[d]
        return rs3[eligible_mask(d) & rs3.notna()].sort_values(ascending=False)

    def rank_sector_nontrend(d):
        rk = sector_rank_mom.loc[d]
        return rk[eligible_mask(d) & rk.notna()].sort_values(ascending=False)

    def nontrend_exposure(avg_rs):
        if pd.isna(avg_rs):
            return 0.0
        ex = 1.00 if avg_rs >= 0.22 else 0.75 if avg_rs >= 0.12 else 0.50 if avg_rs >= 0.05 else 0.25
        return min(ex, NON_TREND_MAX_EXPOSURE)

    def best_real_asset(d):
        price, ma, rs = real_assets.loc[d], real_asset_ma120.loc[d], real_asset_rs_6m.loc[d]
        ok = price.notna() & ma.notna() & rs.notna() & (price > ma) & (rs > REAL_ASSET_RS_THRESHOLD)
        ranked = rs[ok].sort_values(ascending=False)
        return ranked.index[0] if len(ranked) else None

    # 9. 목표 포트폴리오
    def build_targets():
        top3_history = deque(maxlen=RECOVERY_HISTORY_MONTHS)
        targets, rows = {}, []
        for sd in month_end_dates:
            i = dates.get_loc(sd)
            if i < WARMUP:
                continue
            price_now, ma_now = sector_prices.loc[sd], sector_ma120.loc[sd]
            valid = price_now.notna() & ma_now.notna()
            if valid.sum() == 0:
                continue
            breadth = (price_now[valid] > ma_now[valid]).mean()
            r6, r3, rn = rank_sector_6m(sd), rank_sector_3m(sd), rank_sector_nontrend(sd)
            cur_top3 = set(r6.head(3).index)
            pool = set().union(*top3_history) if top3_history else set()
            ndx_price, ndx_ma, ndx_rs = nasdaq100.loc[sd], nasdaq_ma200.loc[sd], nasdaq_vs_sp_rs_6m.loc[sd]
            strong = bool(pd.notna(ndx_price) and pd.notna(ndx_ma) and pd.notna(ndx_rs) and ndx_price > ndx_ma and ndx_rs > NASDAQ_RS_THRESHOLD)
            st = pd.Series(0.0, index=sector_cols)
            nt = pd.Series(0.0, index=nas_names)
            rt = pd.Series(0.0, index=real_names)
            sp_t = ndx_t = final = 0.0
            mode, selected = 'CASH', []
            avg_rs = np.nan
            if strong:                                             # A. 나스닥 독주
                cap = 0.50 if breadth < BREADTH_RECOVERY else 1.00
                internal = nasdaq_internal_rs_6m.loc[sd].dropna().sort_values(ascending=False)
                if len(internal) > 0:
                    w = internal.index[0]
                    nt.loc[w] = cap
                    selected, final = [w], cap
                    mode = 'NASDAQ_INTERNAL_TOP1_CAP50' if breadth < BREADTH_RECOVERY else 'NASDAQ_INTERNAL_TOP1'
                else:
                    ndx_t = final = cap
                    mode = 'NASDAQ100_FALLBACK_CAP50' if breadth < BREADTH_RECOVERY else 'NASDAQ100_FALLBACK'
            elif breadth >= BREADTH_SUPER_BULL:                    # B. 초광범위 강세
                sp_m, nd_m = sp500_mom_1_3_6.loc[sd], nasdaq100_mom_1_3_6.loc[sd]
                if pd.notna(nd_m) and pd.notna(sp_m) and nd_m > sp_m:
                    ndx_t, selected, mode = 1.0, [NASDAQ100_NAME], 'SUPER_BULL_NASDAQ100'
                else:
                    sp_t, selected, mode = 1.0, [SP500_NAME], 'SUPER_BULL_SP500'
                final = 1.0
            elif breadth >= BREADTH_TREND:                         # C. 3개월 RS 상위 2
                selected = list(r3.head(ATTACK_TOP_N).index)
                if selected:
                    st.loc[selected] = 1.0 / len(selected)
                    final = 1.0
                mode = 'RS3M_TOP2'
            elif breadth >= BREADTH_NON_TREND:                     # D. 비추세
                selected = list(rn.head(NON_TREND_TOP_N).index)
                if selected:
                    rs6 = sector_rs_6m.loc[sd, selected].dropna()
                    avg_rs = rs6.mean() if len(rs6) > 0 else np.nan
                base = nontrend_exposure(avg_rs)
                if selected:
                    st.loc[selected] = base / len(selected)
                    final = base
                mode = 'NON_TREND_RANK6M_SKIP1M'
                if base <= 0.25:
                    ra = best_real_asset(sd)
                    if ra is not None:
                        rt.loc[ra] = REAL_ASSET_EXPOSURE
                        selected.append(ra)
                        final = min(1.0, base + REAL_ASSET_EXPOSURE)
                        mode = 'NON_TREND_RANK6M_SKIP1M_METAL'
            elif breadth >= BREADTH_RECOVERY:                      # E. 회복
                dd_now = sector_dd20.loc[sd]
                rec = [n for n in r6.index if n in pool and pd.notna(dd_now.get(n, np.nan))
                       and RECOVERY_DD_MIN <= dd_now[n] <= RECOVERY_DD_MAX][:3]
                if rec:
                    st.loc[rec] = RECOVERY_EXPOSURE / len(rec)
                    selected, final, mode = rec, RECOVERY_EXPOSURE, 'RECOVERY'
                else:
                    ra = best_real_asset(sd)
                    if ra is not None:
                        rt.loc[ra] = REAL_ASSET_EXPOSURE
                        selected, final, mode = [ra], REAL_ASSET_EXPOSURE, 'RECOVERY_METAL'
                    else:
                        mode = 'RECOVERY_CASH'
            else:                                                  # F. 깊은 약세
                sp_t = contrarian_exposure(sp500_dd252.loc[sd])
                final = sp_t
                if sp_t > 0:
                    selected, mode = [SP500_NAME], 'DEEP_WEAKNESS_SP500'
                else:
                    ra = best_real_asset(sd)
                    if ra is not None:
                        rt.loc[ra] = REAL_ASSET_EXPOSURE
                        selected, final, mode = [ra], REAL_ASSET_EXPOSURE, 'DEEP_METAL'
                    else:
                        mode = 'DEEP_CASH'
            if i + 1 < len(dates):
                ed = dates[i + 1]
                targets[ed] = {'sector': st.copy(), 'nas': nt.copy(), 'real': rt.copy(), 'sp': float(sp_t), 'ndx': float(ndx_t)}
            else:
                ed = None
            rows.append({
                'sd': sd, 'ed': ed, 'mode': mode, 'breadth': float(breadth), 'ndx_rs': ndx_rs, 'strong': strong,
                'ndx_gap': (ndx_price / ndx_ma - 1) if pd.notna(ndx_price) and pd.notna(ndx_ma) and ndx_ma else None,
                'dd': sp500_dd252.loc[sd], 'exp': final, 'cash': max(0.0, 1.0 - final), 'sel': list(selected), 'avg_rs': avg_rs,
                'sp_mom': sp500_mom_1_3_6.loc[sd], 'ndx_mom': nasdaq100_mom_1_3_6.loc[sd],
                'top2_3m': list(r3.head(ATTACK_TOP_N).index), 'top3_6m': list(r6.head(3).index), 'top3_nt': list(rn.head(3).index),
                'metal': best_real_asset(sd), 'pool': sorted(pool),
                'valid': int(valid.sum()), 'above': int((price_now[valid] > ma_now[valid]).sum()),
            })
            top3_history.append(cur_top3)
        return rows, targets

    rows, targets = build_targets()

    # 10. 백테스트 — 오늘 수익은 기존 비중으로, 체결일이면 오늘 종가에 교체(비용 반영), 새 비중 수익은 다음 날부터
    SR = sector_returns.reindex(columns=sector_cols).fillna(0.0).to_numpy()
    NR = nasdaq_internal_returns.reindex(columns=nas_names).fillna(0.0).to_numpy()
    RR = real_asset_returns.reindex(columns=real_names).fillna(0.0).to_numpy()
    SPR = sp500_returns.fillna(0.0).to_numpy()
    NDR = nasdaq100_returns.fillna(0.0).to_numpy()
    n = len(dates)
    equity = np.ones(n)
    turnover = np.zeros(n)
    ws, wn, wr = np.zeros(len(sector_cols)), np.zeros(len(nas_names)), np.zeros(len(real_names))
    wsp = wnd = 0.0
    for i in range(1, n):
        prev_eq = equity[i - 1]
        s, nr, rr, sp, nd = SR[i], NR[i], RR[i], SPR[i], NDR[i]
        cash = max(0.0, 1.0 - ws.sum() - wn.sum() - wr.sum() - wsp - wnd)
        mult = cash + float((ws * (1 + s)).sum()) + float((wn * (1 + nr)).sum()) + float((wr * (1 + rr)).sum()) + wsp * (1 + sp) + wnd * (1 + nd)
        cur = prev_eq * mult
        if mult > 0:
            ws, wn, wr = ws * (1 + s) / mult, wn * (1 + nr) / mult, wr * (1 + rr) / mult
            wsp, wnd = wsp * (1 + sp) / mult, wnd * (1 + nd) / mult
        t = targets.get(dates[i])
        if t is not None:
            ts = t['sector'].reindex(sector_cols).fillna(0.0).to_numpy()
            tn = t['nas'].reindex(nas_names).fillna(0.0).to_numpy()
            tr = t['real'].reindex(real_names).fillna(0.0).to_numpy()
            to = float(np.abs(ts - ws).sum() + np.abs(tn - wn).sum() + np.abs(tr - wr).sum() + abs(t['sp'] - wsp) + abs(t['ndx'] - wnd))
            turnover[i] = to
            cur *= max(0.0, 1.0 - TRANSACTION_COST * to)
            ws, wn, wr, wsp, wnd = ts.copy(), tn.copy(), tr.copy(), t['sp'], t['ndx']
        equity[i] = cur
    eq = pd.Series(equity, index=dates)
    to_ser = pd.Series(turnover, index=dates)
    # 지금 들고 있는 것(최신 종가 기준 비중)
    hold_now = [{'t': c, 'w': _num(w * 100, 1)} for c, w in list(zip(sector_cols, ws)) + list(zip(nas_names, wn)) + list(zip(real_names, wr))
                + [(SP500_NAME, wsp), (NASDAQ100_NAME, wnd)] if w > 1e-9]

    # 11. 평가 시작일 — 첫 투자 체결 전날부터 전체 기간(V8.2: 최근 2년 제한 폐지)
    active = sorted(r['ed'] for r in rows if r['exp'] > 0 and r['ed'] is not None)
    if not active:
        raise RuntimeError('투자 신호 없음')
    first_loc = eq.index.get_loc(pd.Timestamp(active[0]))
    if first_loc < 1:
        raise RuntimeError('첫 투자 체결일 이전의 공통 기준일이 없습니다')
    live_start = eq.index[first_loc - 1]
    strat = eq.loc[live_start:] / eq.loc[live_start]
    spy_c = sp500.loc[live_start:] / sp500.loc[live_start]
    qqq_c = nasdaq100.loc[live_start:] / nasdaq100.loc[live_start]

    # 13. 성과
    def stats(curve, tov=None):
        years = (curve.index[-1] - curve.index[0]).days / 365.25
        cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
        ddn = (curve / curve.cummax() - 1).min()
        ret = curve.pct_change().dropna()
        sd_ = ret.std(ddof=1)
        down = ret[ret < 0]
        return {'cagr': _num(cagr), 'mdd': _num(ddn), 'sharpe': _num(ret.mean() / sd_ * np.sqrt(252) if sd_ > 0 else None, 3),
                'sortino': _num(ret.mean() / down.std(ddof=1) * np.sqrt(252) if len(down) > 1 and down.std(ddof=1) > 0 else None, 3),
                'calmar': _num(cagr / abs(ddn) if ddn < 0 else None, 3), 'mult': _num(curve.iloc[-1]),
                'turnover': _num(tov.loc[curve.index[0]:].sum() / years if tov is not None else 0.0, 2)}

    # 14. 연도별
    def yearly(curve):
        out = {}
        for y in sorted(curve.index.year.unique()):
            yc = curve[curve.index.year == y]
            pos = curve.index.get_loc(yc.index[0])
            sv = curve.iloc[pos - 1] if pos > 0 else 1.0
            out[int(y)] = yc.iloc[-1] / sv - 1
        return out
    ys, yp, yq = yearly(strat), yearly(spy_c), yearly(qqq_c)
    years = sorted(ys)

    # 16. 이번 달 확정 / 다음 달 예상
    latest = dates[-1]
    conf = [r for r in rows if r['ed'] is not None and r['ed'] <= latest]
    confirmed = sorted(conf, key=lambda r: (r['ed'], r['sd']))[-1] if conf else rows[-1]
    prev_c = [r for r in rows if r['ed'] is None and r['sd'] <= latest]
    preview = prev_c[-1] if prev_c else rows[-1]

    # 지금 업종 현황(원본 화면엔 없음)
    last = dates[-1]
    r6n, r3n, rnn = rank_sector_6m(last), rank_sector_3m(last), rank_sector_nontrend(last)
    el = eligible_mask(last)
    now = []
    for c in sector_cols:
        p, m = sector_prices.loc[last, c], sector_ma120.loc[last, c]
        now.append({'t': c, 'ma_gap': _num(p / m - 1 if pd.notna(p) and pd.notna(m) and m else None),
                    'rs6': _num(sector_rs_6m.loc[last, c]), 'rs3': _num(sector_rs_3m.loc[last, c]),
                    'rk': _num(sector_rank_mom.loc[last, c]), 'dd20': _num(sector_dd20.loc[last, c]),
                    'elig': bool(el.get(c, False)),
                    'r6': int(list(r6n.index).index(c)) + 1 if c in r6n.index else None,
                    'r3': int(list(r3n.index).index(c)) + 1 if c in r3n.index else None,
                    'rn': int(list(rnn.index).index(c)) + 1 if c in rnn.index else None})
    now.sort(key=lambda x: (x['r6'] is None, x['r6'] or 0, -(x['rs6'] if x['rs6'] is not None else -9)))
    metals = []
    for c in real_names:
        p, m = real_assets.loc[last, c], real_asset_ma120.loc[last, c]
        metals.append({'t': c, 'ma_gap': _num(p / m - 1 if pd.notna(p) and pd.notna(m) and m else None), 'rs6': _num(real_asset_rs_6m.loc[last, c])})
    nas_now = [{'t': c, 'rs6': _num(nasdaq_internal_rs_6m.loc[last, c])} for c in nas_names]

    def sig(r):
        return {'sd': r['sd'].date().isoformat(), 'ed': r['ed'].date().isoformat() if r['ed'] is not None else None, 'mode': r['mode'],
                'breadth': _num(r['breadth']), 'valid': r['valid'], 'above': r['above'], 'ndx_rs': _num(r['ndx_rs']), 'strong': r['strong'],
                'ndx_gap': _num(r['ndx_gap']), 'dd': _num(r['dd']), 'exp': _num(r['exp']), 'cash': _num(r['cash']), 'sel': r['sel'],
                'avg_rs': _num(r['avg_rs']), 'sp_mom': _num(r['sp_mom']), 'ndx_mom': _num(r['ndx_mom']), 'top2_3m': r['top2_3m'],
                'top3_6m': r['top3_6m'], 'top3_nt': r['top3_nt'], 'metal': r['metal'], 'pool': r['pool']}

    gd = strat.index
    return {
        'asof': last.date().isoformat(), 'start': dates[0].date().isoformat(), 'n_etf': int(data.shape[1]),
        'version': VERSION, 'param': {'set': PARAM_SET, **_T}, 'sectors': sector_cols, 'nasdaq': nas_names, 'metals': real_names,
        'confirmed': sig(confirmed), 'preview': sig(preview), 'signals': [sig(r) for r in rows[-RECENT_SIGNALS:]],
        'live_start': live_start.date().isoformat(),
        'perf': {'strat': stats(strat, to_ser), 'spy': stats(spy_c), 'qqq': stats(qqq_c)},
        'yearly': {'years': years, 'strat': [_num(ys.get(y)) for y in years], 'spy': [_num(yp.get(y)) for y in years],
                   'qqq': [_num(yq.get(y)) for y in years]},
        'curve': {'dates': [d.date().isoformat() for d in gd], 'strat': [_num(v) for v in strat], 'spy': [_num(v) for v in spy_c.reindex(gd)],
                  'qqq': [_num(v) for v in qqq_c.reindex(gd)]},
        'hold': hold_now, 'now': now, 'metals_now': metals, 'nasdaq_now': nas_now,
        'index_now': {'spy_dd': _num(sp500_dd252.iloc[-1]), 'ndx_rs': _num(nasdaq_vs_sp_rs_6m.iloc[-1]),
                      'ndx_gap': _num(nasdaq100.iloc[-1] / nasdaq_ma200.iloc[-1] - 1 if pd.notna(nasdaq_ma200.iloc[-1]) else None),
                      'sp_mom': _num(sp500_mom_1_3_6.iloc[-1]), 'ndx_mom': _num(nasdaq100_mom_1_3_6.iloc[-1])},
    }


def run(dry: bool = False) -> dict:
    sb = None if dry else get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[미국월간] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    df = fetch_prices()
    p = compute(df)
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': p['asof'], 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
        cutoff = (date.fromisoformat(p['asof']) - timedelta(days=KEEP_DAYS)).isoformat()
        sb.table(TABLE).delete().eq('kind', KIND).lt('base_date', cutoff).execute()
    c, v = p['confirmed'], p['preview']
    log.info(f"[미국월간] {'dry-run ' if dry else ''}{p['asof']} {VERSION} ETF {p['n_etf']} · 업종 {len(p['sectors'])}"
             f" · 확정 {c['sd']} {c['mode']} {c['exp']} {','.join(c['sel']) or '-'} · 예상 {v['mode']} {v['exp']} {','.join(v['sel']) or '-'}"
             f" · {p['live_start']}부터 {p['perf']['strat']['mult']}배")
    return {'date': p['asof'], 'payload': p}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    ap.add_argument('--dump', help='계산 결과를 이 경로에 JSON으로 저장')
    a = ap.parse_args()
    r = run(dry=a.dry)
    if a.dump and r.get('payload'):
        with open(a.dump, 'w', encoding='utf-8') as f:
            json.dump(r['payload'], f, ensure_ascii=False)
    print({k: v for k, v in r.items() if k != 'payload'})
