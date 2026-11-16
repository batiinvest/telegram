"""미국 업종 ETF 소르티노 · 상대강도 → market_snapshot(kind='us_etf')

[원본] 태린이아빠 Market Dashboard '미국 ETF 소라티노 및 상대강도'(공개 소스 app.py US_SECTOR_SRC).
  · 종목 = SPY(기준) + 업종 · 테마 ETF 99개(12묶음). Yahoo 일봉 3년, 수정 종가.
  · ① Mansfield RS(6개월 = 126거래일)
        상대값 = ETF ÷ SPY,  RS = (상대값 ÷ 상대값의 126일 평균 − 1) × 100
        정규화 = 100 ÷ (1 + e^(−RS ÷ 12))  → 70 이상을 강한 것으로 본다.
     6개월 수익률 = 종가 ÷ 126거래일 전 종가 − 1,  SPY 대비 = ETF 수익률 − SPY 수익률
  · ② 변동성 조정 모멘텀(최근 10거래일 각각 상위 20)
        점수 = 126거래일 구간 수익률(마지막 ÷ 첫 값 − 1) ÷ 같은 구간 일간 수익률 표준편차
        필터 없음 / 종가가 10 · 20 · 60일선 이상인 것만
  · ③ 소르티노(최근 10거래일 각각 상위 20, 종가가 10 · 20 · 50일선 이상인 것만)
        126거래일 일간 수익률로 연율화: (평균 × 252) ÷ (음수 수익률만의 표준편차 × √252).  음수인 날 5개 미만이면 계산 안 함.
  · ④ 과열 점검 — 소르티노(10일선 이상) 최신일 상위 20
        30일 이격도 = (종가 ÷ 30일선 − 1) × 100,  97% 구간 = 최근 247거래일 이격도의 97% 분위수
        위치 = 현재 이격도 ÷ 97% 구간 × 100  (100 이상이면 최근 1년 가운데 상위 3% 과열)
  · ⑤ 이동평균 정배열(4 · 13 · 26 · 52주 = 20 · 65 · 130 · 260거래일)
        이평 정배열 = 20일선 > 65일선 > 130일선 > 260일선,  완전 정배열 = 종가 > 20일선 그리고 이평 정배열
        시작일 · 연속 유지 거래일, 강도 = 네 간격(%)의 합

[원본 대조 — 2026-11-16, 원본 화면 11-13 미국 종가 기준]
  순위표 7종(모멘텀 4 · 소르티노 3)의 최신일 상위 20이 순서까지 모두 같음. 과열 점검 20줄도 같음(SEA · INCO만 0.04%p 차이).
  Mansfield RS는 전 종목이 0.02쯤 낮게 나온다(XLV 14.26 → 14.24) — 원본이 마감 27분 뒤 SPY 값으로 계산한 차이, 순서와 70 이상 3종목은 같음.

[원본과 다른 점] 원본은 관리자가 돌릴 때만 갱신된다. 여기서는 미국 장이 끝난 아침에 돌리고, 장중이면 오늘 봉을 버린다.
  숫자와 순위만 저장하고 표 · 그림은 화면(js/us-etf.js)이 그린다.

[저장] 하루 한 줄(base_date = 마지막 거래일). 과열 점검 그림용 1년 계열이 들어 있어 지난 줄은 10일만 남긴다.

[시각] 매일 07:20(미국 마감 뒤) · 08:30(한 번 더).

실행: python3 collect_us_etf_rs.py [--dry] [--dump 경로]
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
KIND = 'us_etf'
KEEP_DAYS = 10
BENCH = 'SPY'
RS_WINDOW = 126
PERIOD_6M = 126
LAST_N_DAYS = 10
TOP_N = 20
GAP_MA, GAP_LOOKBACK, GAP_PCT = 30, 247, 0.97
MA_4W, MA_13W, MA_26W, MA_52W = 20, 65, 130, 260
NY_CLOSE = (16, 15)

# 원본 industry_etfs — (묶음, {티커: 이름}) 순서 그대로
GROUPS = [
    ('섹터', {'XLK': 'Technology', 'VOX': 'Communication Services', 'XLY': 'Consumer Discretionary', 'XLP': 'Consumer Staples',
              'XLI': 'Industrials', 'XLB': 'Materials', 'XLE': 'Energy', 'XLF': 'Financials', 'XLV': 'Health Care', 'XLU': 'Utilities'}),
    ('테크·반도체·인터넷', {'IGV': 'Software', 'SOXX': 'Semiconductors', 'XSD': 'Semiconductors - Equal Weight', 'PSI': 'Semiconductors - Dynamic',
                           'SKYY': 'Cloud Computing', 'FDN': 'Internet', 'PNQI': 'Internet NASDAQ', 'SOCL': 'Social Media',
                           'QTEC': 'Tech - NASDAQ Equal Weight', 'TDIV': 'Tech Dividend'}),
    ('에너지·원전', {'XOP': 'Oil & Gas Exploration', 'OIH': 'Oil Services', 'IEZ': 'Oil Equipment & Services', 'XES': 'Oil & Gas Services',
                     'FCG': 'Natural Gas', 'AMLP': 'MLP Energy Infrastructure', 'MLPX': 'MLP & Energy Infra',
                     'EMLP': 'North American Energy Infra', 'ENFR': 'Energy Infrastructure', 'URA': 'Uranium', 'NLR': 'Nuclear Energy'}),
    ('금융', {'KBE': 'Banks', 'KRE': 'Regional Banks', 'IAI': 'Broker-Dealers & Exchanges', 'KIE': 'Insurance',
              'IXG': 'Global Financials', 'BIZD': 'Business Development Companies', 'PSP': 'Listed Private Equity'}),
    ('헬스케어·바이오', {'XBI': 'Biotech Equal Weight', 'IBB': 'Biotechnology', 'IHI': 'Medical Devices', 'XHE': 'Health Care Equipment',
                         'IHF': 'Health Care Providers', 'XHS': 'Health Care Services', 'IHE': 'Pharmaceuticals', 'PJP': 'Pharma Dynamic',
                         'ARKG': 'Genomic Revolution', 'GNOM': 'Genomics & Biotechnology'}),
    ('소비·주택·레저', {'XRT': 'Retail', 'XHB': 'Homebuilders', 'ITB': 'Home Construction', 'PEJ': 'Leisure & Entertainment',
                        'PBJ': 'Food & Beverage', 'RXI': 'Global Consumer Discretionary', 'CHIQ': 'China Consumer',
                        'ECON': 'Emerging Markets Consumer', 'INCO': 'India Consumer'}),
    ('산업·방산·운송', {'ITA': 'Aerospace & Defense', 'PPA': 'Defense & Aerospace', 'XAR': 'Aerospace & Defense Equal Weight',
                        'IYT': 'Transportation', 'XTN': 'Transportation Equal Weight', 'TOLZ': 'Global Infrastructure',
                        'GII': 'Global Infrastructure SPDR', 'EMIF': 'Emerging Markets Infrastructure', 'CGW': 'Global Water',
                        'FIW': 'Water Resources', 'AIRR': 'American Industrial Renaissance'}),
    ('소재·자원·금속', {'GNR': 'Natural Resources', 'GUNR': 'Global Upstream Natural Resources', 'HAP': 'Natural Resources Producers',
                        'GDX': 'Gold Miners', 'GDXJ': 'Junior Gold Miners', 'SGDM': 'Gold Miners - Sprott', 'SIL': 'Silver Miners',
                        'SILJ': 'Junior Silver Miners', 'SLVP': 'Global Silver Miners', 'SLX': 'Steel', 'COPX': 'Copper Miners',
                        'REMX': 'Rare Earth & Strategic Metals', 'PICK': 'Global Metal & Mining', 'MOO': 'Agribusiness',
                        'VEGI': 'Agriculture Producers', 'WOOD': 'Global Timber Forestry', 'CUT': 'Timber & Forestry'}),
    ('클린에너지·그리드', {'ICLN': 'Global Clean Energy', 'PBW': 'Clean Energy - WilderHill', 'PBD': 'Global Clean Energy', 'TAN': 'Solar Energy',
                           'FAN': 'Wind Energy', 'QCLN': 'Clean Energy NASDAQ', 'GRID': 'Smart Grid'}),
    ('통신', {'IYZ': 'Telecom', 'XTL': 'Telecom SPDR', 'IXP': 'Global Communication Services'}),
    ('기타 테마', {'LIT': 'Lithium & Battery Tech', 'SEA': 'Shipping & Cargo', 'CARZ': 'Global Auto', 'UFO': 'Space & Satellite'}),
]
NAMES = {t: n for _, m in GROUPS for t, n in m.items()}
GROUP_OF = {t: g for g, m in GROUPS for t in m}
ETFS = list(NAMES)


def _has_schema(sb) -> bool:
    try:
        sb.table(TABLE).select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def _num(v, nd=2):
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


def fetch_prices(now_utc=None):
    """Yahoo 수정 종가 3년 → DataFrame(날짜 × 티커, SPY 포함). 미국 장중이면 오늘 봉은 버린다."""
    import pandas as pd
    import yfinance as yf

    data = yf.download([BENCH] + ETFS, period='3y', auto_adjust=True, progress=False, group_by='column', threads=False)['Close']
    data.index = pd.DatetimeIndex(data.index).tz_localize(None).normalize()
    data = data.sort_index()
    ny = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/New_York'))
    if len(data) and data.index[-1].date() == ny.date() and (ny.hour, ny.minute) < NY_CLOSE:
        data = data.iloc[:-1]
    return data.dropna(how='all')


def sortino_from_returns(ret, mar=0.0, periods_per_year=252):
    """원본 sortino_from_returns — 연율화 소르티노."""
    import numpy as np

    ret = ret.dropna()
    if len(ret) < 5:
        return np.nan
    excess = ret - ((1 + mar) ** (1 / periods_per_year) - 1)
    downside = excess[excess < 0]
    if len(downside) < 5:
        return np.nan
    dev = downside.std(ddof=1) * np.sqrt(periods_per_year)
    if dev == 0 or np.isnan(dev):
        return np.nan
    return float(excess.mean() * periods_per_year / dev)


def _ma_ok(close, end_idx, ma_days):
    """end_idx 시점에서 종가 ≥ 이동평균(ma_days)인지."""
    ok = {}
    for t in close.columns:
        s = close[t].iloc[:end_idx + 1].dropna()
        ok[t] = bool(len(s) >= ma_days and s.iloc[-1] >= s.rolling(ma_days).mean().iloc[-1])
    return ok


def rank_momentum(data, ma_filter_days=None):
    """② 변동성 조정 모멘텀 — 최근 10거래일 각각 상위 20. 반환 (날짜별 티커 목록, 최신일 점수)."""
    import numpy as np

    days, last_scores = {}, {}
    for day in data.index[-LAST_N_DAYS:]:
        end = data.index.get_loc(day)
        win = data.iloc[max(0, end - PERIOD_6M + 1):end + 1]
        if len(win) < int(PERIOD_6M * 0.8):
            days[day] = []
            continue
        score = (win.iloc[-1] / win.iloc[0] - 1) / win.pct_change(fill_method=None).std().replace(0, np.nan)
        score = score.drop(labels=[BENCH], errors='ignore')
        if ma_filter_days is not None:
            ok = _ma_ok(data.drop(columns=[BENCH], errors='ignore'), end, ma_filter_days)
            score = score[[t for t in score.index if ok.get(t, False)]]
        score = score.dropna().sort_values(ascending=False)
        days[day] = score.head(TOP_N).index.tolist()
        last_scores = {t: _num(v, 2) for t, v in score.head(TOP_N).items()}
    return days, last_scores


def rank_sortino(close, ma_filter_days):
    """③ 소르티노 — 최근 10거래일 각각 상위 20(이동평균선 이상만)."""
    import pandas as pd

    days, last_scores = {}, {}
    for day in close.dropna(how='all').index[-LAST_N_DAYS:]:
        end = close.index.get_loc(day)
        ok = _ma_ok(close, end, ma_filter_days)
        scores = {}
        for t in close.columns:
            if not ok.get(t, False):
                continue
            prices = close[t].iloc[max(0, end - PERIOD_6M + 1):end + 1].dropna()
            if len(prices) < max(20, int(PERIOD_6M * 0.8)):
                continue
            sc = sortino_from_returns(prices.pct_change(fill_method=None).dropna())
            if pd.notna(sc):
                scores[t] = float(sc)
        ranked = pd.Series(scores, dtype=float).sort_values(ascending=False).head(TOP_N)
        days[day] = ranked.index.tolist()
        last_scores = {t: _num(v, 2) for t, v in ranked.items()}
    return days, last_scores


def _streak(mask):
    """최신일 기준 True가 이어진 거래일 수와 시작일."""
    import numpy as np

    mask = mask.dropna().astype(bool)
    if mask.empty or not bool(mask.iloc[-1]):
        return 0, None
    false_pos = np.flatnonzero(~mask.to_numpy())
    start = 0 if len(false_pos) == 0 else false_pos[-1] + 1
    return int(len(mask) - start), mask.index[start]


def compute(data) -> dict:
    import numpy as np
    import pandas as pd

    if BENCH not in data.columns:
        raise ValueError('SPY 데이터가 없습니다')
    iso = lambda d: d.date().isoformat()
    spy = data[BENCH]
    sector = data.drop(columns=[BENCH], errors='ignore')

    # ① Mansfield RS + 6개월 수익률
    ret6 = data / data.shift(PERIOD_6M) - 1
    latest_6m = ret6.dropna(how='all').index[-1]
    etfs = []
    for t in ETFS:
        if t not in data.columns:
            continue
        row = {'t': t, 'name': NAMES[t], 'g': GROUP_OF[t], 'rs': None, 'norm': None}
        aligned = pd.concat([data[t].dropna(), spy], axis=1, join='inner')
        aligned.columns = ['etf', 'spy']
        if len(aligned) >= RS_WINDOW:
            rel = aligned['etf'] / aligned['spy']
            rs = ((rel / rel.rolling(RS_WINDOW, min_periods=RS_WINDOW).mean()) - 1) * 100
            rs = rs.dropna()
            if not rs.empty:
                raw = round(float(rs.iloc[-1]), 2)
                row['rs'], row['norm'] = raw, round(100 * (1 / (1 + np.exp(-raw / 12))), 2)
        r6 = ret6.loc[latest_6m, t]
        row['ret6m'] = _num(r6 * 100) if pd.notna(r6) else None
        row['vs_spy'] = _num((r6 - ret6.loc[latest_6m, BENCH]) * 100) if pd.notna(r6) and pd.notna(ret6.loc[latest_6m, BENCH]) else None
        row['last'] = _num(data[t].dropna().iloc[-1], 2) if data[t].notna().any() else None
        etfs.append(row)

    # ② ③ 순위표
    def pack(days, scores):
        ds = sorted(days)
        return {'dates': [iso(d) for d in ds], 'ranks': [days[d] for d in ds], 'scores': scores}

    lists = {
        'mom': pack(*rank_momentum(data)),
        'mom10': pack(*rank_momentum(data, 10)),
        'mom20': pack(*rank_momentum(data, 20)),
        'mom60': pack(*rank_momentum(data, 60)),
        'sor10': pack(*rank_sortino(sector, 10)),
        'sor20': pack(*rank_sortino(sector, 20)),
        'sor50': pack(*rank_sortino(sector, 50)),
    }

    # ④ 과열 점검 — 소르티노(10일선 이상) 최신일 상위 20
    heat = []
    heat_days = sector.index[-GAP_LOOKBACK:]            # 그림용 계열은 공통 날짜에 맞춰 한 번만 싣는다
    for rank_no, t in enumerate((lists['sor10']['ranks'] or [[]])[-1][:TOP_N], start=1):
        price = sector[t].dropna()
        if len(price) < GAP_MA + 20:
            continue
        ma30 = price.rolling(GAP_MA).mean()
        gap = ((price / ma30 - 1) * 100).dropna()
        if len(gap) < 20:
            continue
        hist = gap.tail(GAP_LOOKBACK)
        cur, p97 = float(gap.iloc[-1]), float(hist.quantile(GAP_PCT))
        heat.append({'rank': rank_no, 't': t, 'date': iso(gap.index[-1]), 'gap': _num(cur), 'p97': _num(p97),
                     'heat': _num(cur / p97 * 100, 1) if pd.notna(p97) and p97 != 0 else None, 'n': int(len(hist)),
                     'price': [_num(v, 2) if pd.notna(v) else None for v in price.reindex(heat_days)],
                     'gaps': [_num(v, 2) if pd.notna(v) else None for v in gap.reindex(heat_days)]})

    # ⑤ 이동평균 정배열
    ma = {n: data.rolling(n, min_periods=n).mean() for n in (MA_4W, MA_13W, MA_26W, MA_52W)}
    align = []
    for t in ETFS:
        if t not in data.columns:
            continue
        temp = pd.DataFrame({'p': data[t], 'a': ma[MA_4W][t], 'b': ma[MA_13W][t], 'c': ma[MA_26W][t], 'd': ma[MA_52W][t]}).dropna()
        if temp.empty:
            continue
        ma_mask = (temp['a'] > temp['b']) & (temp['b'] > temp['c']) & (temp['c'] > temp['d'])
        full_mask = (temp['p'] > temp['a']) & ma_mask
        r, latest = temp.iloc[-1], temp.index[-1]
        ma_days, ma_start = _streak(ma_mask)
        full_days, full_start = _streak(full_mask)
        gaps = [(r['p'] / r['a'] - 1) * 100, (r['a'] / r['b'] - 1) * 100, (r['b'] / r['c'] - 1) * 100, (r['c'] / r['d'] - 1) * 100]
        align.append({'t': t, 'date': iso(latest),
                      'ma': bool(ma_mask.iloc[-1]), 'ma_start': iso(ma_start) if ma_start is not None else None, 'ma_days': ma_days,
                      'full': bool(full_mask.iloc[-1]), 'full_start': iso(full_start) if full_start is not None else None, 'full_days': full_days,
                      'above4w': bool(r['p'] > r['a']), 'gaps': [_num(g) for g in gaps], 'strength': _num(sum(gaps))})

    return {
        'asof': iso(data.index[-1]), 'asof_6m': iso(latest_6m),
        'n': len(etfs), 'total': len(ETFS),
        'spy': {'ret6m': _num(ret6.loc[latest_6m, BENCH] * 100), 'last': _num(spy.dropna().iloc[-1], 2)},
        'groups': [g for g, _ in GROUPS],
        'etfs': etfs, 'lists': lists, 'heat': heat, 'heat_dates': [iso(d) for d in heat_days], 'align': align,
    }


def run(dry: bool = False) -> dict:
    sb = get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[미국ETF] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    data = fetch_prices()
    if data.shape[1] < (len(ETFS) + 1) * 0.8 or len(data) < 400:
        raise RuntimeError(f'야후 수신 부족 — 종목 {data.shape[1]}/{len(ETFS) + 1}, {len(data)}일')
    p = compute(data)
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': p['asof'], 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
        cutoff = (datetime.fromisoformat(p['asof']) - timedelta(days=KEEP_DAYS)).date().isoformat()
        sb.table(TABLE).delete().eq('kind', KIND).lt('base_date', cutoff).execute()
    strong = sum(1 for e in p['etfs'] if (e['norm'] or 0) >= 70)
    full = sum(1 for a in p['align'] if a['full'])
    top = (p['lists']['sor10']['ranks'] or [[]])[-1][:3]
    log.info(f"[미국ETF] {'dry-run ' if dry else ''}{p['asof']} ETF {p['n']}/{p['total']} · 정규화 RS 70 이상 {strong}개 · "
             f"완전 정배열 {full}개 · 소르티노(10일선) 상위 {', '.join(top)}")
    return {'date': p['asof'], 'n': p['n'], 'strong': strong, 'full': full, 'payload': p}


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
