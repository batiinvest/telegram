"""AI 하드웨어 주가 모멘텀(Breadth · 피크아웃 점검) → market_snapshot(kind='ai_hw')

[원본] 태린이아빠 Market Dashboard 'AI하드웨어 주가 모멘텀 점검'(공개 소스 app.py AI_SRC — AI Tech Breadth & Peak-out Dashboard).
  · 종목 41개 = 미국 26 · 한국 3 · 일본 8 · 대만/ADR 4. Yahoo 일봉 3년, 수정 종가(auto_adjust=True).
  · 나라별 휴장일 차이는 앞 값으로 5일까지 메운다(ffill limit 5). 값이 220일 미만인 종목은 뺀다(200일선 계산용).
  · 기간별 상승·하락 종목 수: 21 · 42 · 63 · 126거래일 수익률의 부호.
  · Breadth(날마다, 값이 있는 종목만)
      20 · 60 · 200일선 위 종목 비율(이동평균 최소 15 · 45 · 160일), 60일 수익률이 플러스인 종목 비율
      Breadth Score = 20일선 20% + 60일선 35% + 200일선 25% + 60일 수익률 20%
      MA5 = Score의 5일 평균(최소 3일), 기울기 3 · 5 · 10일 = MA5 − n일 전 MA5, 최근 10일 가운데 MA5가 내린 날 수
  · 동일가중 지수 = 종목마다 첫 값을 100으로 맞춘 뒤 평균.
  · 다이버전스(지수는 고점권인데 Breadth는 약해짐)
      지수 20일 고점권 = 지수 ≥ 20일 최고 × 0.99,  Breadth 20일 고점 대비 = MA5 − MA5의 20일 최고
      약세 다이버전스     = 고점권 ∧ 고점 대비 ≤ −10 ∧ 5일 기울기 < 0
      강한 약세 다이버전스 = 고점권 ∧ 고점 대비 ≤ −20 ∧ 10일 기울기 < 0
  · 위험 단계(위에서부터 먼저 맞는 것)
      강한 다이버전스 → 피크아웃 강경고 / 다이버전스 → 피크아웃 경고
      MA5 ≥ 70: 5일 기울기 ≥ 0 강세 · < 0 고점 경계 / ≥ 60 정상 / ≥ 50: 5일 기울기 < 0 주의 · 아니면 중립
      ≥ 35: 10일 기울기 < 0 위험 · 아니면 약세 반등 / < 35: 5일 기울기 < 0 투매 진행 · ≥ 0 투매 후 반등

[원본과 다른 점] 원본 화면은 그림 5장만 보인다(표 · 글은 계산만 하고 숨김). 여기서는 숨겨 둔 현재 요약 · 위험 단계 · 최근 30일 표도 저장해 화면이 보여 준다.
  원본은 관리자가 아무 때나 돌린다. 여기서는 미국 장이 끝난 아침에 돌리고, 장이 열려 있는 나라의 오늘 봉은 버린다.

[저장] 하루 한 줄(base_date = 계산 기준일). 묶음에 3년 치 계열이 통째로 들어가므로 지난 줄은 10일만 남긴다.

[시각] 매일 07:00(미국 마감 뒤) · 08:20(한 번 더).

실행: python3 collect_ai_hw.py [--dry] [--dump 경로]
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
KIND = 'ai_hw'
KEEP_DAYS = 10
MINIMUM_DATA = 220
PERIODS = {'1M': 21, '2M': 42, '3M': 63, '6M': 126}
RECENT_ROWS = 30

# 원본 stocks — (이름, 티커, 지역)
STOCKS = [
    ('NVDA', 'NVDA', 'US'), ('AVGO', 'AVGO', 'US'), ('AMD', 'AMD', 'US'), ('TSM', 'TSM', 'US'), ('ASML', 'ASML', 'US'),
    ('MU', 'MU', 'US'), ('ARM', 'ARM', 'US'), ('QCOM', 'QCOM', 'US'), ('MRVL', 'MRVL', 'US'), ('LRCX', 'LRCX', 'US'),
    ('AMAT', 'AMAT', 'US'), ('KLAC', 'KLAC', 'US'), ('CDNS', 'CDNS', 'US'), ('SNPS', 'SNPS', 'US'), ('ANET', 'ANET', 'US'),
    ('TXN', 'TXN', 'US'), ('ON', 'ON', 'US'), ('DELL', 'DELL', 'US'),
    ('Sandisk', 'SNDK', 'US'), ('Intel', 'INTC', 'US'), ('Amkor', 'AMKR', 'US'), ('WesternDigital', 'WDC', 'US'),
    ('Seagate', 'STX', 'US'), ('Lumentum', 'LITE', 'US'), ('Corning', 'GLW', 'US'), ('AsteraLabs', 'ALAB', 'US'),
    ('Samsung', '005930.KS', 'KR'), ('SKHynix', '000660.KS', 'KR'), ('SamsungElectroMechanics', '009150.KS', 'KR'),
    ('TokyoElectron', '8035.T', 'JP'), ('Advantest', '6857.T', 'JP'), ('Disco', '6146.T', 'JP'), ('Lasertec', '6920.T', 'JP'),
    ('SCREEN', '7735.T', 'JP'), ('Socionext', '6526.T', 'JP'), ('Murata', '6981.T', 'JP'), ('Kioxia', '285A.T', 'JP'),
    ('UMC', 'UMC', 'TW'), ('ASE', 'ASX', 'TW'), ('Himax', 'HIMX', 'TW'), ('SiliconMotion', 'SIMO', 'TW'),
]
# 거래소별 (시간대, 마감 시·분) — 마감 + 15분 전이면 오늘 봉은 진행 중
MARKET = {'.KS': ('Asia/Seoul', (15, 30)), '.T': ('Asia/Tokyo', (15, 30)), '': ('America/New_York', (16, 0))}
CLOSE_BUFFER_MIN = 15


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
    """Yahoo 수정 종가 3년 → DataFrame(날짜 × 이름). 장이 열려 있는 거래소의 오늘 봉은 버린다."""
    import numpy as np
    import pandas as pd
    import yfinance as yf

    tickers = [t for _, t, _ in STOCKS]
    raw = yf.download(tickers, period='3y', auto_adjust=True, progress=False, group_by='column', threads=False)
    if raw is None or raw.empty:
        raise RuntimeError('야후 일봉을 받지 못했습니다')
    prices = raw['Close'].copy()
    prices.index = pd.DatetimeIndex(prices.index).tz_localize(None).normalize()
    now_utc = now_utc or datetime.now(timezone.utc)
    for suffix, (tz, (hh, mm)) in MARKET.items():
        local = now_utc.astimezone(ZoneInfo(tz))
        if (local.hour, local.minute) >= divmod(hh * 60 + mm + CLOSE_BUFFER_MIN, 60):
            continue
        key = pd.Timestamp(local.date())
        cols = [t for t in tickers if (t.endswith(suffix) if suffix else '.' not in t) and t in prices.columns]
        if key in prices.index and cols:
            prices.loc[key, cols] = np.nan
    prices = prices.dropna(how='all')
    prices = prices.rename(columns={t: n for n, t, _ in STOCKS}).sort_index()
    return prices[[n for n, _, _ in STOCKS if n in prices.columns]]


def compute(prices) -> dict:
    """원본 AI_SRC 2 ~ 15."""
    import numpy as np
    import pandas as pd

    prices = prices.ffill(limit=5)                                   # 나라별 휴장일 차이
    valid = prices.columns[prices.notna().sum() >= MINIMUM_DATA]
    excluded = [c for c in prices.columns if c not in valid]
    prices = prices[valid]

    # 3. 기간별 상승·하락 종목 수
    counts = []
    for name, days in PERIODS.items():
        r = prices.pct_change(days, fill_method=None).iloc[-1].dropna()
        counts.append({'p': name, 'pos': int((r > 0).sum()), 'neg': int((r < 0).sum()),
                       'zero': int((r == 0).sum()), 'total': int(len(r))})

    # 4. Breadth
    ma20 = prices.rolling(20, min_periods=15).mean()
    ma60 = prices.rolling(60, min_periods=45).mean()
    ma200 = prices.rolling(200, min_periods=160).mean()
    ret60 = prices.pct_change(60, fill_method=None)

    def share(cond, avail):
        return cond.sum(axis=1) / avail.sum(axis=1).replace(0, np.nan) * 100

    b = pd.DataFrame({
        'a20': share((prices > ma20) & prices.notna() & ma20.notna(), prices.notna() & ma20.notna()),
        'a60': share((prices > ma60) & prices.notna() & ma60.notna(), prices.notna() & ma60.notna()),
        'a200': share((prices > ma200) & prices.notna() & ma200.notna(), prices.notna() & ma200.notna()),
        'p60': share((ret60 > 0) & ret60.notna(), ret60.notna()),
    })
    # 5. Breadth Score
    b['score'] = b['a20'] * 0.20 + b['a60'] * 0.35 + b['a200'] * 0.25 + b['p60'] * 0.20
    b['ma5'] = b['score'].rolling(5, min_periods=3).mean()
    b['diff'] = b['score'] - b['ma5']
    b['s3'] = b['ma5'] - b['ma5'].shift(3)
    b['s5'] = b['ma5'] - b['ma5'].shift(5)
    b['s10'] = b['ma5'] - b['ma5'].shift(10)
    b['fall10'] = b['ma5'].diff().lt(0).rolling(10).sum()
    # 6. 동일가중 지수
    first = prices.apply(lambda col: col.dropna().iloc[0] if not col.dropna().empty else np.nan)
    b['index'] = (prices / first * 100).mean(axis=1)
    b['index_20d'] = b['index'].pct_change(20) * 100
    b['index_60d'] = b['index'].pct_change(60) * 100
    # 7. 다이버전스
    b['near_high'] = b['index'] >= b['index'].rolling(20).max() * 0.99
    b['from_high'] = b['ma5'] - b['ma5'].rolling(20).max()
    b['div'] = b['near_high'] & (b['from_high'] <= -10) & (b['s5'] < 0)
    b['sdiv'] = b['near_high'] & (b['from_high'] <= -20) & (b['s10'] < 0)

    # 8. 위험 단계
    def risk(row):
        score, s5, s10 = row['ma5'], row['s5'], row['s10']
        if pd.isna(score):
            return 'nodata'
        if row['sdiv']:
            return 'peak_strong'
        if row['div']:
            return 'peak_warn'
        if score >= 70 and s5 >= 0:
            return 'strong'
        if score >= 70 and s5 < 0:
            return 'top_watch'
        if score >= 60:
            return 'normal'
        if score >= 50:
            return 'caution' if s5 < 0 else 'neutral'
        if score >= 35:
            return 'risk' if s10 < 0 else 'weak_rebound'
        if score < 35 and s5 < 0:
            return 'selloff'
        if score < 35 and s5 >= 0:
            return 'selloff_rebound'
        return 'neutral'

    b['risk'] = b.apply(risk, axis=1)

    ok = b.dropna(subset=['ma5'])
    latest, latest_date = ok.iloc[-1], ok.index[-1]
    keys = ['a20', 'a60', 'a200', 'p60', 'score', 'ma5', 'diff', 's3', 's5', 's10', 'fall10', 'from_high',
            'index', 'index_20d', 'index_60d']
    iso = lambda d: d.date().isoformat()

    # 종목별 현재 위치(원본 계산의 재료 — 어느 종목이 선 위·아래인지)
    last = prices.iloc[-1]
    stocks = []
    for name, ticker, region in STOCKS:
        if name not in prices.columns:
            stocks.append({'n': name, 't': ticker, 'r': region, 'excluded': True})
            continue
        row = {'n': name, 't': ticker, 'r': region, 'last': _num(last[name], 2)}
        for pname, days in PERIODS.items():
            s = prices[name]
            row[pname] = _num((s.iloc[-1] / s.iloc[-1 - days] - 1) * 100) if len(s) > days and pd.notna(s.iloc[-1 - days]) else None
        for k, ma in (('m20', ma20), ('m60', ma60), ('m200', ma200)):
            row[k] = None if pd.isna(ma[name].iloc[-1]) or pd.isna(last[name]) else bool(last[name] > ma[name].iloc[-1])
        stocks.append(row)

    series_cols = ['a20', 'a60', 'a200', 'score', 'ma5', 's5', 's10', 'index']
    return {
        'asof': iso(latest_date),
        'n': int(len(prices.columns)), 'total': len(STOCKS), 'excluded': excluded,
        'period': [iso(prices.index.min()), iso(prices.index.max())],
        'counts': counts,
        'latest': {**{k: _num(latest[k]) for k in keys}, 'risk': latest['risk'],
                   'div': bool(latest['div']), 'sdiv': bool(latest['sdiv']), 'near_high': bool(latest['near_high'])},
        'series': {'dates': [iso(d) for d in b.index],
                   **{c: [_num(v, 2) for v in b[c]] for c in series_cols},
                   'div': [iso(d) for d in b.index[b['div'].fillna(False)]],
                   'sdiv': [iso(d) for d in b.index[b['sdiv'].fillna(False)]]},
        'recent': [{'d': iso(d), **{k: _num(r[k]) for k in ('a20', 'a60', 'a200', 'p60', 'ma5', 'diff', 's5', 's10', 'from_high')},
                    'div': bool(r['div']), 'sdiv': bool(r['sdiv']), 'risk': r['risk']}
                   for d, r in b.tail(RECENT_ROWS).iterrows()],
        'stocks': stocks,
    }


def run(dry: bool = False) -> dict:
    sb = get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[AI하드웨어] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    prices = fetch_prices()
    if prices.shape[1] < len(STOCKS) * 0.8 or len(prices) < 400:
        raise RuntimeError(f'야후 수신 부족 — 종목 {prices.shape[1]}/{len(STOCKS)}, {len(prices)}일')
    p = compute(prices)
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': p['asof'], 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
        cutoff = (datetime.fromisoformat(p['asof']) - timedelta(days=KEEP_DAYS)).date().isoformat()
        sb.table(TABLE).delete().eq('kind', KIND).lt('base_date', cutoff).execute()   # 3년 계열이 통째로 들어 있어 지난 줄은 조금만
    la = p['latest']
    log.info(f"[AI하드웨어] {'dry-run ' if dry else ''}{p['asof']} 종목 {p['n']}/{p['total']} · Breadth MA5 {la['ma5']} "
             f"(5일 {la['s5']:+.1f} · 10일 {la['s10']:+.1f}) · 단계 {la['risk']}"
             f"{' · 제외 ' + ', '.join(p['excluded']) if p['excluded'] else ''}")
    return {'date': p['asof'], 'n': p['n'], 'risk': la['risk'], 'ma5': la['ma5'], 'payload': p}


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
