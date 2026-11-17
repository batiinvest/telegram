"""미국 52주 신고가 전략(52W FIXED + Rotation_B) → market_snapshot(kind='us_52w')

[원본] 태린이아빠 Market Dashboard '미국 52주 신고가 전략 점검'(공개 소스 app.py ROT_SRC · run_rotation).
  · 종목 = SPY(기준) + 미국 업종 ETF 57개 + QQQ(약세 판정에만). Yahoo 일봉 3년, 수정 종가.
  · 지표(전일 종가 신호 → 그날 종가 체결 가정, 수익은 다음 거래일부터)
      RS(6개월)     = (ETF ÷ SPY) ÷ 126거래일 전 (ETF ÷ SPY) − 1
      52주 돌파      = 종가 > 직전 252거래일 종가 최고
      매도           = 종가 < 20일선
  · 52W FIXED(순수 52주 신고가)
      ① 보유 종목이 20일선 아래면 판다  ② RS 상위 20 가운데 52주 돌파한 것을 RS 순으로
      ③ 같은 묶음은 하나만(GDX·SIL·COPX·REMX = 광산, ICLN·TAN·FAN·GRID = 클린에너지)
      ④ 최대 4종목, 종목당 min(25%, 남은 현금). 거래비용 0.1% × 회전율. 비중은 매일 가격대로 움직인다.
  · 약세 국면 판정: 52W FIXED 자산의 최근 20거래일 수익이 SPY보다 1%p 이상 **그리고** QQQ보다 1%p 이상 뒤지면 WEAK_ROTATION.
      화면의 '통함 / 안통함'은 최신일 이 판정의 반대다.
  · 52W + Rotation_B(최종 전략): 매도는 같다. 빈 자리를 채우는 후보만 국면에 따라 바뀐다.
      NORMAL_52W   → 위 52W 후보
      WEAK_ROTATION → 5일 수익 > SPY 5일 ∧ 20일 SPY 대비 순위가 10거래일 전보다 10계단 이상 개선 ∧ 종가 > 직전 20일 최고
                      (개선폭 큰 순 → 5일 RS 강한 순 → 현재 20일 순위 순)
      국면이 바뀌어도 기존 보유는 팔지 않는다.
  · 최신일 신호는 자산 곡선에 소급하지 않고 '현재 포트'에만 반영한다.
  · 성과 비교 시작일 = 두 전략이 처음 거래한 날 가운데 늦은 날(그 전날 = 1). CAGR · MDD · Sharpe · Sortino · Calmar · 배수.

[원본 대조 — 2026-11-17, 원본 화면 11-16 미국 종가 기준(원본 갱신 11-16 21:24 UTC)]
  상태 '통함' · SPY 대비 +3.02% · QQQ 대비 +7.44% · NORMAL_52W, 현재 포트 4종목(XLP 26.48 · XLV 25.03 · IHE 24.43 · XLU 24.06,
  출처 ROTATION_B · 52W · 52W · ROTATION_B), 오늘 매매 없음, 성과표 3전략 × 6칸이 모두 같음.

[가격 검증 — 원본과 같음] SOXX · ITA 2026-11-05 3:1 분할 주변의 단위 혼재를 보정하고, 그래도 일간 |수익률| 50% 초과나
  0 이하 가격이 있는 ETF는 매매 대상에서 뺀다. SPY · QQQ가 걸리면 계산을 멈춘다.
  원본은 yfinance의 자동 보정(repair=True)도 켜지만 서버에 scipy가 없어 쓸 수 없다(켜면 빈 결과) — 위 두 단계만 쓴다.

[원본과 다른 점] 원본 화면은 상태 · 현재 포트 · 오늘 매매 · 성과표 · 자산 곡선만 보인다. 여기서는 원본 Colab이 출력하는
  연도별 수익 · 국면 일수 · 최근 거래와, 판정 근거(오늘 RS 상위 20과 돌파 여부, 보유 종목의 20일선 거리, 약세 후보)도 저장한다.

[저장] 하루 한 줄(base_date = 마지막 거래일). 지난 줄은 10일만 남긴다.

[시각] 매일 07:30(미국 마감 뒤) · 08:40(한 번 더).

실행: python3 collect_us_52w.py [--dry] [--dump 경로]
"""

import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client

log = get_logger(__name__)

TABLE = 'market_snapshot'
KIND = 'us_52w'
KEEP_DAYS = 10
NY_CLOSE = (16, 15)

BENCH, REGIME_BENCH = 'SPY', 'QQQ'
RS_LOOKBACK, HIGH_LOOKBACK, SELL_MA_DAYS, RS_TOP_N = 126, 252, 20, 20
MAX_HOLDINGS, MAX_WEIGHT, COST, USE_GROUP_LIMIT = 4, 0.25, 0.001, True
WEAK_LOOKBACK, WEAK_THRESHOLD = 20, -0.01
ROT_RS_SHORT, ROT_RS_MEDIUM, ROT_RANK_LOOKBACK, ROT_RANK_IMPROVE, ROT_BREAKOUT = 5, 20, 10, 10, 20
EPS = 1e-10
KNOWN_FORWARD_SPLITS = {'SOXX': ('2026-11-05', 3.0), 'ITA': ('2026-11-05', 3.0)}
SPLIT_SCAN_DAYS_BEFORE, SPLIT_SCAN_DAYS_AFTER, SPLIT_RATIO_TOLERANCE = 14, 7, (0.65, 1.35)
ABNORMAL_DAILY_RETURN_LIMIT = 0.50
RECENT_TRADES = 30

# 원본 industry_etfs — 순서 그대로(SPY는 기준)
ETFS = {
    'SPY': 'S&P 500', 'XLK': 'Technology', 'XLC': 'Communication Services', 'XLY': 'Consumer Discretionary',
    'XLP': 'Consumer Staples', 'XLI': 'Industrials', 'XLB': 'Materials', 'XLE': 'Energy', 'XLF': 'Financials',
    'XLV': 'Health Care', 'XLU': 'Utilities', 'XLRE': 'Real Estate', 'SOXX': 'Semiconductors', 'IGV': 'Software',
    'SKYY': 'Cloud Computing', 'FDN': 'Internet', 'BUG': 'Cybersecurity', 'XOP': 'Oil & Gas Exploration',
    'OIH': 'Oil Services', 'CRAK': 'Oil Refining', 'AMLP': 'Energy Infrastructure', 'URA': 'Uranium',
    'KRE': 'Regional Banks', 'IAI': 'Broker Dealers & Exchanges', 'KIE': 'Insurance', 'BIZD': 'Business Development Companies',
    'IBB': 'Biotechnology', 'IHI': 'Medical Devices', 'IHF': 'Health Care Providers', 'IHE': 'Pharmaceuticals',
    'ARKG': 'Genomic Revolution', 'GNOM': 'Genomics & Biotechnology', 'XRT': 'Retail', 'XHB': 'Homebuilders',
    'PEJ': 'Leisure & Entertainment', 'PBJ': 'Food & Beverage', 'ITA': 'Aerospace & Defense', 'IYT': 'Transportation',
    'TOLZ': 'Global Infrastructure', 'CGW': 'Global Water', 'AIRR': 'US Industrial Renaissance', 'GDX': 'Gold Miners',
    'SIL': 'Silver Miners', 'SLX': 'Steel', 'COPX': 'Copper Miners', 'REMX': 'Rare Earth & Strategic Metals',
    'MOO': 'Agribusiness', 'WOOD': 'Timber & Forestry', 'ICLN': 'Global Clean Energy', 'TAN': 'Solar Energy',
    'FAN': 'Wind Energy', 'GRID': 'Smart Grid', 'IYZ': 'Telecom', 'LIT': 'Lithium & Battery Tech', 'SEA': 'Shipping',
    'CARZ': 'Global Auto', 'UFO': 'Space & Satellite', 'ROBO': 'Robotics & Automation',
}
GROUP_MAP = {'GDX': 'Mining', 'SIL': 'Mining', 'COPX': 'Mining', 'REMX': 'Mining',
             'ICLN': 'Clean_Energy', 'TAN': 'Clean_Energy', 'FAN': 'Clean_Energy', 'GRID': 'Clean_Energy'}
for _t in ETFS:
    GROUP_MAP.setdefault(_t, _t)
ALL_TICKERS = list(dict.fromkeys(list(ETFS) + [REGIME_BENCH]))


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


# ── 가격 ──────────────────────────────────────────────────────────────

def fetch_prices(now_utc=None):
    """Yahoo 수정 종가 3년(SPY · QQQ · ETF 57). 미국 장중이면 오늘 봉은 버린다."""
    import pandas as pd
    import yfinance as yf

    raw = yf.download(ALL_TICKERS, period='3y', interval='1d', auto_adjust=True, progress=False, threads=False, group_by='column')
    if raw is None or raw.empty:
        raise RuntimeError('야후 일봉을 받지 못했습니다')
    prices = raw['Close'].copy()
    prices.index = pd.DatetimeIndex(prices.index).tz_localize(None).normalize()
    prices = prices.loc[:, ~prices.columns.duplicated()].sort_index().dropna(how='all')
    ny = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/New_York'))
    if len(prices) and prices.index[-1].date() == ny.date() and (ny.hour, ny.minute) < NY_CLOSE:
        prices = prices.iloc[:-1]
    return prices


def repair_declared_splits(raw_prices):
    """원본 repair_declared_splits — 공식 분할일 주변의 '가격 단위 혼재'만 보정. 남은 불연속이 있으면 그 ETF를 막는다."""
    import numpy as np
    import pandas as pd

    corrected = raw_prices.copy()
    events, blocked = [], {}
    all_market_date = pd.Timestamp(raw_prices.index.max()).normalize()
    for ticker, (eff, ratio) in KNOWN_FORWARD_SPLITS.items():
        if ticker not in corrected.columns:
            continue
        s = pd.to_numeric(corrected[ticker], errors='coerce').dropna()
        if s.empty:
            continue
        split_date = pd.Timestamp(eff)
        latest = pd.Timestamp(s.index[-1]).normalize()
        if all_market_date >= split_date and latest < split_date:
            blocked[ticker] = f'분할일 {split_date.date()} 이후 유효 종가 없음'
            continue
        if latest < split_date:
            continue
        raw_values = s.to_numpy(dtype=float)
        idx = pd.DatetimeIndex(s.index)
        factors = np.ones(len(s), dtype=float)
        applied = 1.0
        first_day = split_date - pd.Timedelta(days=SPLIT_SCAN_DAYS_BEFORE)
        last_day = split_date + pd.Timedelta(days=SPLIT_SCAN_DAYS_AFTER)
        lower, upper = SPLIT_RATIO_TOLERANCE
        for i in range(len(s) - 2, -1, -1):
            prev_raw, next_raw = raw_values[i], raw_values[i + 1]
            prev_factor = applied
            nearby = first_day <= idx[i + 1].normalize() <= last_day and (idx[i + 1] - idx[i]).days <= 7
            if nearby and prev_raw > 0 and next_raw > 0:
                rr = prev_raw / next_raw
                if rr > 2.0 and lower <= rr / ratio <= upper:
                    prev_factor = applied / ratio
                elif rr < 0.5 and lower <= rr * ratio <= upper:
                    prev_factor = applied * ratio
            if not np.isclose(prev_factor, applied):
                events.append({'t': ticker, 'boundary': idx[i + 1].date().isoformat(), 'ratio': round(float(prev_raw / next_raw), 4)})
            factors[i] = prev_factor
            applied = prev_factor
        repaired = s * factors
        corrected.loc[s.index, ticker] = repaired.to_numpy()
        window = repaired.loc[(repaired.index >= first_day) & (repaired.index <= last_day)]
        jumps = window.pct_change(fill_method=None).abs() > ABNORMAL_DAILY_RETURN_LIMIT
        if jumps.any():
            blocked[ticker] = '분할 보정 뒤에도 분할일 주변 비정상 변동: ' + ', '.join(str(x.date()) for x in jumps[jumps].index)
    return corrected, events, blocked


def audit_all_etf_prices(price_data, preblocked=None):
    """원본 audit_all_etf_prices — 일간 |수익률| 50% 초과 · 0 이하 가격이 있으면 매매 대상에서 뺀다(가격은 고치지 않음)."""
    import pandas as pd

    blocked = dict(preblocked or {})
    for ticker in price_data.columns:
        valid = pd.to_numeric(price_data[ticker], errors='coerce').dropna()
        if valid.empty:
            blocked[ticker] = '유효 가격 없음'
            continue
        if (valid <= 0).any():
            blocked[ticker] = '0 이하 가격'
            continue
        ret = valid.pct_change(fill_method=None)
        bad = ret[ret.abs() > ABNORMAL_DAILY_RETURN_LIMIT]
        if not bad.empty:
            blocked[ticker] = '일간 50% 초과 변동: ' + ', '.join(f'{d.date()} {r:+.1%}' for d, r in bad.iloc[:3].items())
    return blocked


# ── 계산 ──────────────────────────────────────────────────────────────

def compute(raw_prices) -> dict:
    import numpy as np
    import pandas as pd

    prices, split_events, quarantined = repair_declared_splits(raw_prices)
    quarantined = audit_all_etf_prices(prices, quarantined)
    for b in (BENCH, REGIME_BENCH):
        if b in quarantined:
            raise RuntimeError(f'기준지수 {b} 가격 오류: {quarantined[b]}')
        if b not in prices.columns:
            raise RuntimeError(f'{b} 다운로드 실패')

    dates = prices[BENCH].dropna().index
    prices = prices.reindex(dates).ffill(limit=3)
    cols = [t for t in ETFS if t != BENCH and t not in quarantined and t in prices.columns
            and prices[t].notna().sum() >= HIGH_LOOKBACK + 5]
    if not cols:
        raise RuntimeError('가격 검증을 통과한 업종 ETF가 없습니다')
    N, T = len(cols), len(dates)
    sp = prices[cols]
    spy, qqq = prices[BENCH], prices[REGIME_BENCH]
    sec_ret = sp.pct_change(fill_method=None)
    spy_ret = spy.pct_change(fill_method=None)

    # 지표 — 원본 6 · 7
    rs = sp.div(spy, axis=0)
    rs = rs / rs.shift(RS_LOOKBACK) - 1
    prev_high = sp.shift(1).rolling(HIGH_LOOKBACK, min_periods=HIGH_LOOKBACK).max()
    brk = sp > prev_high
    sell_ma = sp.rolling(SELL_MA_DAYS, min_periods=SELL_MA_DAYS).mean()
    below = sp < sell_ma
    rs5 = (sp / sp.shift(ROT_RS_SHORT) - 1).sub(spy / spy.shift(ROT_RS_SHORT) - 1, axis=0)
    rs20 = (sp / sp.shift(ROT_RS_MEDIUM) - 1).sub(spy / spy.shift(ROT_RS_MEDIUM) - 1, axis=0)
    rank20 = rs20.rank(axis=1, ascending=False, method='min')
    improve = rank20.shift(ROT_RANK_LOOKBACK) - rank20
    high20 = sp.shift(1).rolling(ROT_BREAKOUT, min_periods=ROT_BREAKOUT).max()
    brk20 = sp > high20
    rot_sig = (rs5 > 0) & (improve >= ROT_RANK_IMPROVE) & brk20

    RS, BRK, BELOW, ROT = rs.to_numpy(), brk.to_numpy(), below.to_numpy(), rot_sig.to_numpy()
    IMP, RS5, RK20 = improve.to_numpy(), rs5.to_numpy(), rank20.to_numpy()
    RET = sec_ret.fillna(0.0).to_numpy()
    group = [GROUP_MAP[t] for t in cols]

    def rs_order(s):
        """그날 RS 내림차순(값 있는 것만) → (상위 20 위치 목록, {위치: 순위})."""
        ser = pd.Series(RS[s], index=range(N)).dropna().sort_values(ascending=False)
        return list(ser.head(RS_TOP_N).index), {j: r for r, j in enumerate(ser.index, start=1)}

    def cands_52w(s, remaining, sold):
        top, rank_map = rs_order(s)
        c = [j for j in top if j not in remaining and j not in sold and bool(BRK[s, j])]
        return sorted(c, key=lambda j: rank_map.get(j, 999)), rank_map

    def cands_rot(s, remaining, sold):
        c = [j for j in range(N) if j not in remaining and j not in sold and bool(ROT[s, j])]

        def key(j):
            imp = float(IMP[s, j]) if not np.isnan(IMP[s, j]) else -999.0
            r5 = float(RS5[s, j]) if not np.isnan(RS5[s, j]) else -999.0
            rk = float(RK20[s, j]) if not np.isnan(RK20[s, j]) else 999.0
            return (-imp, -r5, rk)
        return sorted(c, key=key)

    def step(s, w, origins, regime_fn, trades, exec_i):
        """원본 신호 적용 — 매도(20일선 이탈) → 빈 자리 매수. w · origins는 그 자리에서 바뀐다."""
        sells = [j for j in range(N) if w[j] > EPS and bool(BELOW[s, j])]
        for j in sells:
            trades.append({'s': s, 'e': exec_i, 'j': j, 'a': 'SELL', 'o': origins.pop(j, ''), 'w': -float(w[j]), 'r': 'ma'})
            w[j] = 0.0
        remaining = [j for j in range(N) if w[j] > EPS]
        slots = max(0, MAX_HOLDINGS - len(remaining))
        weak, regime = regime_fn(s)
        if weak:
            cands, origin, rank_map = cands_rot(s, remaining, sells), 'ROTATION_B', None
        else:
            (cands, rank_map), origin = cands_52w(s, remaining, sells), '52W'
        used = {group[j] for j in remaining}
        buys = []
        for j in cands:
            if len(buys) >= slots:
                break
            if USE_GROUP_LIMIT and group[j] in used:
                continue
            bw = min(MAX_WEIGHT, max(0.0, 1.0 - float(w.sum())))
            if bw <= EPS:
                break
            w[j] = bw
            origins[j] = origin
            buys.append(j)
            used.add(group[j])
            trades.append({'s': s, 'e': exec_i, 'j': j, 'a': 'BUY', 'o': origin, 'w': bw,
                           'r': 'rs52' if origin == '52W' else 'rot', 'rk': (rank_map or {}).get(j)})
        if float(w.sum()) > 1.0 + 1e-8:
            raise RuntimeError(f'투자비중 100% 초과 {dates[s].date()}')
        return sells, buys, regime, origin

    def backtest(regime_fn, first):
        equity = np.ones(T)
        w = np.zeros(N)
        origins, trades, regimes = {}, [], ['WARMUP'] * T
        for i in range(1, T):
            s = i - 1
            prev_eq = equity[i - 1]
            if s >= first:
                old = w.copy()
                _, _, regimes[i], _ = step(s, w, origins, regime_fn, trades, i)
                turnover = float(np.abs(w - old).sum())
                if turnover > 0:
                    prev_eq *= max(0.0, 1.0 - COST * turnover)
            r = RET[i]
            mult = max(0.0, 1.0 - float(w.sum())) + float((w * (1 + r)).sum())
            equity[i] = prev_eq * mult
            if mult > 0:
                w = w * (1 + r) / mult
        # 최신일 신호 — 자산에는 소급하지 않음
        live_w, live_o, live_t = w.copy(), dict(origins), []
        sells, buys, regime, origin = step(T - 1, live_w, live_o, regime_fn, live_t, T - 1)
        return {'equity': pd.Series(equity, index=dates), 'trades': trades, 'live_trades': live_t, 'regimes': regimes,
                'final_w': live_w, 'origins': live_o, 'sells': sells, 'buys': buys, 'regime': regime, 'origin': origin}

    # ① 순수 52W FIXED — 이 자산 곡선이 약세 판정의 기준
    base = backtest(lambda s: (False, '52W'), max(RS_LOOKBACK, HIGH_LOOKBACK, SELL_MA_DAYS))
    b20 = base['equity'] / base['equity'].shift(WEAK_LOOKBACK) - 1
    ex_spy = (b20 - (spy / spy.shift(WEAK_LOOKBACK) - 1)).to_numpy()
    ex_qqq = (b20 - (qqq / qqq.shift(WEAK_LOOKBACK) - 1)).to_numpy()

    def regime_of(s):
        weak = bool(not np.isnan(ex_spy[s]) and not np.isnan(ex_qqq[s]) and ex_spy[s] <= WEAK_THRESHOLD and ex_qqq[s] <= WEAK_THRESHOLD)
        return weak, 'WEAK_ROTATION' if weak else 'NORMAL_52W'

    # ② 최종 52W + Rotation_B
    final = backtest(regime_of, max(HIGH_LOOKBACK, RS_LOOKBACK, SELL_MA_DAYS, WEAK_LOOKBACK,
                                    ROT_RS_MEDIUM + ROT_RANK_LOOKBACK, ROT_BREAKOUT))

    # 같은 시작일 비교 — 두 전략의 첫 체결일 가운데 늦은 날, 그 전날 = 1
    # 최신일(T−1)에 체결된 것은 빼고 본다(원본: Execution_Date < 마지막 날)
    starts = [min(t['e'] for t in x['trades'] if t['e'] < T - 1) for x in (base, final) if any(t['e'] < T - 1 for t in x['trades'])]
    if not starts:
        raise RuntimeError('비교할 수 있는 거래가 없습니다')
    start = max(starts)

    def norm(curve):
        return curve.iloc[start:] / curve.iloc[start - 1 if start > 0 else start]

    spy_full = (1 + spy_ret.fillna(0.0)).cumprod()
    curves = {'final': norm(final['equity']), 'base': norm(base['equity']), 'spy': norm(spy_full)}

    def stats(curve):
        curve = curve.dropna()
        r = curve.pct_change().dropna()
        years = (curve.index[-1] - curve.index[0]).days / 365.25
        cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan
        mdd = (curve / curve.cummax() - 1).min()
        sharpe = np.sqrt(252) * r.mean() / r.std() if r.std() > 0 else np.nan
        down = r[r < 0].std()
        sortino = np.sqrt(252) * r.mean() / down if pd.notna(down) and down > 0 else np.nan
        calmar = cagr / abs(mdd) if mdd < 0 else np.nan
        return {'cagr': _num(cagr * 100, 2), 'mdd': _num(mdd * 100, 2), 'sharpe': _num(sharpe, 2), 'sortino': _num(sortino, 2),
                'calmar': _num(calmar, 2), 'mult': _num(curve.iloc[-1] / curve.iloc[0], 4)}

    def yearly(curve):
        r = curve.pct_change().fillna(0.0)
        return ((1 + r).groupby(r.index.year).prod() - 1)

    yr = pd.concat([yearly(curves['final']), yearly(curves['base']), yearly(curves['spy'])], axis=1)
    iso = lambda d: pd.Timestamp(d).date().isoformat()
    reg_window = final['regimes'][start:]
    live = T - 1

    def trade_out(t):
        j = t['j']
        return {'sd': iso(dates[t['s']]), 'ed': iso(dates[t['e']]), 't': cols[j], 'a': t['a'], 'o': t['o'],
                'w': _num(t['w'] * 100, 2), 'r': t['r'], 'rk': t.get('rk')}

    # 판정 근거 — 최신일 RS 상위 20 · 보유 종목의 20일선 거리 · 약세 국면 후보
    top, rank_map = rs_order(live)
    held_now = [j for j in range(N) if final['final_w'][j] > EPS]
    sma = sell_ma.to_numpy()
    PX = sp.to_numpy()
    ph = prev_high.to_numpy()
    gap = lambda j: _num((PX[live, j] / sma[live, j] - 1) * 100, 2) if not np.isnan(sma[live, j]) else None

    portfolio = sorted([{'t': cols[j], 'o': final['origins'].get(j, ''), 'w': _num(final['final_w'][j] * 100, 2),
                         'ma_gap': gap(j), 'rs': _num(RS[live, j] * 100, 2), 'rs_rank': rank_map.get(j)}
                        for j in held_now], key=lambda x: -(x['w'] or 0))
    invested = float(final['final_w'][held_now].sum()) if held_now else 0.0
    weak_now = bool(not np.isnan(ex_spy[live]) and not np.isnan(ex_qqq[live])
                    and ex_spy[live] <= WEAK_THRESHOLD and ex_qqq[live] <= WEAK_THRESHOLD)
    status = '판정 대기' if np.isnan(ex_spy[live]) or np.isnan(ex_qqq[live]) else ('안통함' if weak_now else '통함')

    return {
        'asof': iso(dates[live]),
        'status': status, 'regime': final['regime'], 'buy_mode': final['origin'],
        'ex_spy': _num(ex_spy[live] * 100, 2), 'ex_qqq': _num(ex_qqq[live] * 100, 2), 'threshold': WEAK_THRESHOLD * 100,
        'portfolio': portfolio, 'invested': _num(invested * 100, 2), 'cash': _num(max(0.0, 1 - invested) * 100, 2),
        'actions': [{'a': 'SELL', 't': cols[j]} for j in final['sells']] + [{'a': 'BUY', 't': cols[j]} for j in final['buys']],
        'base_actions': [{'a': 'SELL', 't': cols[j]} for j in base['sells']] + [{'a': 'BUY', 't': cols[j]} for j in base['buys']],
        'base_portfolio': [{'t': cols[j], 'w': _num(base['final_w'][j] * 100, 2)} for j in range(N) if base['final_w'][j] > EPS],
        'rs_top': [{'t': cols[j], 'rank': rank_map.get(j), 'rs': _num(RS[live, j] * 100, 2), 'brk': bool(BRK[live, j]),
                    'to_high': _num((PX[live, j] / ph[live, j] - 1) * 100, 2) if not np.isnan(ph[live, j]) else None,
                    'held': j in held_now, 'g': group[j] if group[j] != cols[j] else None} for j in top],
        'rot_cands': [{'t': cols[j], 'imp': _num(IMP[live, j], 0), 'rs5': _num(RS5[live, j] * 100, 2), 'rank20': _num(RK20[live, j], 0)}
                      for j in cands_rot(live, held_now, [])],
        'perf': {'final': stats(curves['final']), 'base': stats(curves['base']), 'spy': stats(curves['spy'])},
        'yearly': {'years': [int(y) for y in yr.index],
                   'final': [_num(v * 100, 2) for v in yr.iloc[:, 0]], 'base': [_num(v * 100, 2) for v in yr.iloc[:, 1]],
                   'spy': [_num(v * 100, 2) for v in yr.iloc[:, 2]]},
        'regime_days': {k: reg_window.count(k) for k in ('NORMAL_52W', 'WEAK_ROTATION')},
        'start': iso(dates[start]),
        'curve': {'dates': [iso(d) for d in dates[start:]],
                  'final': [_num(v, 4) for v in curves['final']], 'base': [_num(v, 4) for v in curves['base']],
                  'spy': [_num(v, 4) for v in curves['spy']],
                  'weak': [1 if r == 'WEAK_ROTATION' else 0 for r in reg_window],
                  'ex_spy': [_num(v * 100, 2) for v in ex_spy[start - 1:live]],     # 그날 국면을 정한 신호일(전일) 값
                  'ex_qqq': [_num(v * 100, 2) for v in ex_qqq[start - 1:live]]},
        'trades': [trade_out(t) for t in (final['trades'] + final['live_trades'])[-RECENT_TRADES:]],
        'n': N, 'total': len(ETFS) - 1,
        'quarantined': quarantined, 'split_events': split_events,
        'names': {t: ETFS[t] for t in cols},
    }


def run(dry: bool = False) -> dict:
    sb = get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[미국52주] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    raw = fetch_prices()
    if raw.shape[1] < len(ALL_TICKERS) * 0.8 or len(raw) < 400:
        raise RuntimeError(f'야후 수신 부족 — 종목 {raw.shape[1]}/{len(ALL_TICKERS)}, {len(raw)}일')
    p = compute(raw)
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': p['asof'], 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
        cutoff = (datetime.fromisoformat(p['asof']) - timedelta(days=KEEP_DAYS)).date().isoformat()
        sb.table(TABLE).delete().eq('kind', KIND).lt('base_date', cutoff).execute()
    port = ', '.join(f"{x['t']} {x['w']:.0f}%" for x in p['portfolio']) or '현금 100%'
    acts = ', '.join(f"{a['a']} {a['t']}" for a in p['actions']) or '없음'
    log.info(f"[미국52주] {'dry-run ' if dry else ''}{p['asof']} {p['status']}({p['regime']}) · SPY 대비 {p['ex_spy']:+.2f}%p · "
             f"QQQ 대비 {p['ex_qqq']:+.2f}%p · 포트 {port} · 오늘 {acts}"
             f"{' · 격리 ' + ', '.join(p['quarantined']) if p['quarantined'] else ''}")
    return {'date': p['asof'], 'status': p['status'], 'regime': p['regime'], 'payload': p}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    ap.add_argument('--dump', help='계산 결과를 이 경로에 JSON으로 저장')
    a = ap.parse_args()
    r = run(dry=a.dry)
    if a.dump and r.get('payload'):
        with open(a.dump, 'w', encoding='utf-8') as f:
            json.dump(r['payload'], f, ensure_ascii=False)
    print({k: v for k, v in r.items() if k != 'payload'})
