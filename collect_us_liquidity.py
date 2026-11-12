"""미국 유동성 체크 → us_liquidity_daily (하루 한 줄, 계산 결과 묶음)

[원본] 태린이아빠 Market Dashboard '미국 유동성 체크'(공개 소스 app.py LIQ_SRC)를 식 그대로 옮김.
  FRED 15종(2023-01-01부터):
    금리        DFII10(10년 실질) · DGS2 · DGS10 · DGS30
    Fed/재무부  WALCL(Fed 자산) · WTREGEN(TGA) · RRPONTSYD(RRP)
    민간 신용   TOTLL(은행대출) · WRESBAL(지급준비금) · M2SL
    엔캐리      DEXJPUS · VIXCLS · NIKKEI225 · SAHMREALTIME · IRSTCI01JPM156N(일본 단기금리, 보조 — 없어도 계산)
  단위: WALCL · WTREGEN · WRESBAL 백만 달러 → 조 달러(÷1e6), RRPONTSYD · TOTLL · M2SL 십억 달러 → 조 달러(÷1e3).
  Liquidity Proxy = Fed 자산 − TGA − RRP.
  주간 = 금요일 끝 주(W-FRI)의 마지막 값(빈 주는 앞 값). 지금 = 마지막 주, 4주 전 = 뒤에서 5번째, 1년 전 = 뒤에서 53번째.
  M2는 월간이라 3개월 연율 = ((지금 ÷ 3개월 전)^4 − 1) · YoY.

  Liquidity Score(4점)  실질금리↓ / 2Y↓ / 10Y−2Y 확대이면서 2Y↓ / Liquidity Proxy↑        (모두 최근 4주 변화)
  Private Credit(2점)   은행대출 4주 · YoY 둘 다 + / M2 3개월 연율 · YoY 둘 다 +
  경고등                지급준비금 4주 ≤ −5% 경고 · < 0 주의 / RRP < 0.05조 거의 소진 · < 0.20조 낮음
  엔캐리(6점)           엔 5일 ≤ −2.5% 또는 4주 ≤ −5% / 미 2Y 4주 ≤ −0.30%p / 미·일 금리차 4주 ≤ −0.25%p /
                        VIX ≥ 25 또는 5일 +50% / 닛케이 5일 ≤ −7% / Sahm ≥ 0.40
                        (5일 · 4주는 달력 7일 · 28일 전 이하의 마지막 값과 비교)
                        엔 신호가 없으면 다른 신호가 있어도 '낮음'.
  2024-08-05 유사도     엔 4주 · 미 2Y 4주 · VIX(15 초과분) · 닛케이 5일 · Sahm의 스트레스를 당시 값으로 나눠(0~1)
                        가중합(0.30 · 0.25 · 0.20 · 0.15 · 0.10) × 100.

[원본 대조 — 2026-11-12] 원본 화면의 숫자 43개(금리 6 · Fed 4 · 민간 신용 3 · 4주 변화 13 · 엔캐리 현재 12 ·
  2024-08-05 기준 5)와 점수(2/4 · 2/2 · 엔캐리 1/6 · 유사도 13%) · 판정 13종이 모두 같음.

[저장] 숫자와 판정 코드만 저장하고, 문장은 화면(js/us-market.js)이 원본 문구로 붙인다.
  base_date = 계산한 날(한국 날짜). 같은 날 다시 돌리면 덮어쓴다.

[받기] fred.stlouisfed.org/graph/fredgraph.csv — 열쇠 없이 받는다.
  ⚠️ User-Agent를 'Mozilla/5.0'으로 보내면 서버에서 응답이 오지 않는다(2026-11-12 확인). requests 기본값으로 보낼 것.

[시각] 07:10(FRED 금리 갱신 = 미국 16:15 뒤) · 12:10(한 번 더).

실행: python3 collect_us_liquidity.py [--dry]
"""

import io
import json
import math
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client

log = get_logger(__name__)

TABLE = 'us_liquidity_daily'
FRED_CSV = 'https://fred.stlouisfed.org/graph/fredgraph.csv'
START_DATE = '2023-01-01'      # 원본 START_DATE
EVENT_DATE = '2024-08-05'      # 원본 엔캐리 청산 기준 사건
PAUSE = 0.3

SERIES = {
    'Real10Y': 'DFII10', 'Treasury2Y': 'DGS2', 'Treasury10Y': 'DGS10', 'Treasury30Y': 'DGS30',
    'FedAssets': 'WALCL', 'TGA': 'WTREGEN', 'RRP': 'RRPONTSYD',
    'BankLoans': 'TOTLL', 'Reserves': 'WRESBAL', 'M2': 'M2SL',
    'USDJPY': 'DEXJPUS', 'VIX': 'VIXCLS', 'Nikkei225': 'NIKKEI225', 'Sahm': 'SAHMREALTIME',
    'JapanCall': 'IRSTCI01JPM156N',
}
REQUIRED = ['Real10Y', 'Treasury2Y', 'Treasury10Y', 'Treasury30Y', 'FedAssets', 'TGA', 'RRP',
            'BankLoans', 'Reserves', 'M2', 'USDJPY', 'VIX', 'Nikkei225', 'Sahm']
CARRY_WEIGHTS = {'JPY': 0.30, 'US2Y': 0.25, 'VIX': 0.20, 'Nikkei': 0.15, 'Sahm': 0.10}


def _has_schema(sb) -> bool:
    try:
        sb.table(TABLE).select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def fetch_fred(code: str, tries: int = 3):
    """FRED 한 계열 → pandas Series(날짜 색인). 빈 값('.')은 NaN."""
    import pandas as pd
    import requests

    err = None
    for i in range(tries):
        try:
            r = requests.get(FRED_CSV, params={'id': code, 'cosd': START_DATE}, timeout=30)
            r.raise_for_status()
            d = pd.read_csv(io.StringIO(r.text), na_values='.')
            if d.shape[1] < 2 or d.empty:
                raise ValueError(f'응답 형식이 다릅니다: {r.text[:80]!r}')
            s = pd.Series(pd.to_numeric(d.iloc[:, 1], errors='coerce').values,
                          index=pd.to_datetime(d.iloc[:, 0]), name=code)
            return s[s.index >= pd.Timestamp(START_DATE)].sort_index()
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f'FRED {code} 받기 실패: {err}')


def fetch_all():
    import pandas as pd

    got = {}
    for name, code in SERIES.items():
        try:
            got[name] = fetch_fred(code)
        except Exception as e:
            if name in REQUIRED:
                raise
            log.warning(f'[미국유동성] {name}({code}) 받기 실패 — 보조 지표라 빼고 계산: {e}')
        time.sleep(PAUSE)
    return pd.concat(got, axis=1).sort_index()


def _num(v, nd=4):
    """NaN → None, 그 밖은 반올림한 float."""
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


def compute(df) -> dict:
    """원본 LIQ_SRC 계산 — 반환은 저장할 묶음(숫자 + 판정 코드)."""
    import numpy as np
    import pandas as pd

    df = df.copy()
    nan = lambda x: pd.isna(x)

    # 2. 단위 변환(조 달러)
    df['FedAssets_T'] = df['FedAssets'] / 1_000_000
    df['TGA_T'] = df['TGA'] / 1_000_000
    df['Reserves_T'] = df['Reserves'] / 1_000_000
    df['RRP_T'] = df['RRP'] / 1_000
    df['BankLoans_T'] = df['BankLoans'] / 1_000
    df['M2_T'] = df['M2'] / 1_000
    # 3 · 4. 금리차 · Liquidity Proxy
    df['Spread10_2'] = df['Treasury10Y'] - df['Treasury2Y']
    df['Spread30_10'] = df['Treasury30Y'] - df['Treasury10Y']
    df['NetLiquidity'] = df['FedAssets_T'] - df['TGA_T'] - df['RRP_T']

    # 5. 주간
    weekly_cols = ['Real10Y', 'Treasury2Y', 'Treasury10Y', 'Treasury30Y', 'Spread10_2', 'Spread30_10',
                   'FedAssets_T', 'TGA_T', 'RRP_T', 'NetLiquidity', 'BankLoans_T', 'Reserves_T']
    weekly = df[weekly_cols].resample('W-FRI').last().ffill()
    if len(weekly) < 53:
        raise ValueError('YoY 및 최근 4주 변화를 계산하기 위한 데이터가 부족합니다.')
    now, prev4, prev52 = weekly.iloc[-1], weekly.iloc[-5], weekly.iloc[-53]

    # 6 · 7. 최근 4주 변화
    real_change = now['Real10Y'] - prev4['Real10Y']
    y2_change = now['Treasury2Y'] - prev4['Treasury2Y']
    y10_change = now['Treasury10Y'] - prev4['Treasury10Y']
    y30_change = now['Treasury30Y'] - prev4['Treasury30Y']
    spread10_2_change = now['Spread10_2'] - prev4['Spread10_2']
    spread30_10_change = now['Spread30_10'] - prev4['Spread30_10']
    liq_change = now['NetLiquidity'] - prev4['NetLiquidity']
    bankloan_4w_pct = (now['BankLoans_T'] / prev4['BankLoans_T'] - 1) * 100
    bankloan_yoy = (now['BankLoans_T'] / prev52['BankLoans_T'] - 1) * 100
    reserve_4w_change = now['Reserves_T'] - prev4['Reserves_T']
    reserve_4w_pct = (now['Reserves_T'] / prev4['Reserves_T'] - 1) * 100

    # 8. M2 (월간)
    m2_monthly = df['M2_T'].dropna().resample('ME').last().dropna()
    if len(m2_monthly) < 13:
        raise ValueError('M2 YoY 계산을 위한 데이터가 부족합니다.')
    m2_now, m2_prev3, m2_prev12 = m2_monthly.iloc[-1], m2_monthly.iloc[-4], m2_monthly.iloc[-13]
    m2_3m_ann = ((m2_now / m2_prev3) ** 4 - 1) * 100
    m2_yoy = (m2_now / m2_prev12 - 1) * 100
    m2_date = m2_monthly.index[-1]

    # 9. Liquidity Score (4점)
    score = 0
    sig_real = bool(real_change < 0)
    sig_y2 = bool(y2_change < 0)
    score += sig_real + sig_y2
    if spread10_2_change > 0 and y2_change < 0:
        score += 1
        sig_steep = 'good'          # 2Y 하락형 스티프닝
    elif spread10_2_change > 0 and y10_change > 0:
        sig_steep = 'bad'           # 10Y 상승형 스티프닝
    else:
        sig_steep = 'none'
    sig_liq = bool(liq_change > 0)
    score += sig_liq

    # 11. Private Credit Score (2점)
    credit_score = 0
    if bankloan_4w_pct > 0 and bankloan_yoy > 0:
        credit_score += 1
        sig_loan = 'up'
    elif bankloan_4w_pct <= 0 and bankloan_yoy > 0:
        sig_loan = 'slow'
    else:
        sig_loan = 'down'
    if m2_3m_ann > 0 and m2_yoy > 0:
        credit_score += 1
        sig_m2 = 'up'
    elif m2_yoy > 0:
        sig_m2 = 'slow'
    else:
        sig_m2 = 'down'

    # 12 · 13. 지급준비금 · RRP
    reserve = 'warn' if reserve_4w_pct <= -5 else 'caution' if reserve_4w_pct < 0 else 'ok'
    rrp = 'empty' if now['RRP_T'] < 0.05 else 'low' if now['RRP_T'] < 0.20 else 'ok'

    # 14. Fed + Private Credit 종합
    if score >= 3 and credit_score == 2:
        total = 'best'
    elif score <= 1 and credit_score == 0:
        total = 'worst'
    elif score <= 1 and credit_score == 2:
        total = 'shift'
    elif score >= 3 and credit_score == 0:
        total = 'policy'
    elif score == 2 and credit_score == 2:
        total = 'good'
    else:
        total = 'mixed'

    # 15. 기존 종합 매크로 해석
    if real_change < 0 and y2_change < 0 and liq_change > 0:
        macro = 'riskon'
    elif real_change < 0 and y2_change < 0 and liq_change <= 0:
        macro = 'easing_noliq'
    elif real_change > 0 and liq_change < 0:
        macro = 'tight'
    elif liq_change > 0 and real_change > 0:
        macro = 'offset'
    else:
        macro = 'mixed'

    # 16. 30Y / 30Y−10Y
    if y30_change < 0 and spread30_10_change < 0:
        long_rate = 'best'
    elif y30_change < 0 and y10_change < 0:
        long_rate = 'good'
    elif y10_change < 0 and y30_change >= 0:
        long_rate = 'caution'
    elif y30_change > 0 and spread30_10_change > 0:
        long_rate = 'bad'
    else:
        long_rate = 'neutral'

    # 17. 금리 구조 종합
    if real_change < 0 and y2_change < 0 and y30_change < 0 and spread30_10_change < 0:
        rate_struct = 'all_ease'
    elif y2_change < 0 and y10_change < 0 and y30_change >= 0:
        rate_struct = 'no30'
    elif y2_change < 0 and y30_change > 0 and spread30_10_change > 0:
        rate_struct = 'split'
    elif y2_change > 0 and real_change > 0 and y30_change > 0:
        rate_struct = 'all_tight'
    else:
        rate_struct = 'mixed'

    # 18. 자산별 해석
    if real_change < 0 and y30_change < 0:
        gold = 'best'
    elif real_change < 0:
        gold = 'good'
    else:
        gold = 'bad'
    if (liq_change > 0 or credit_score == 2) and real_change < 0 and y2_change < 0:
        btc = 'best'
    elif real_change < 0 and y2_change < 0:
        btc = 'good'
    elif liq_change < 0 and credit_score == 0:
        btc = 'caution'
    else:
        btc = 'neutral'
    if y2_change < 0 and credit_score == 2 and y30_change < 0:
        small = 'best'
    elif y2_change < 0 and credit_score == 2:
        small = 'good'
    elif y2_change < 0 and real_change < 0:
        small = 'maybe'
    else:
        small = 'wait'

    # 19. 엔캐리 청산 위험
    def clean(name):
        return df[name].dropna().sort_index()

    def latest_before(name, date=None):
        s = clean(name)
        if date is not None:
            s = s[s.index <= pd.Timestamp(date)]
        return np.nan if len(s) == 0 else float(s.iloc[-1])

    def value_days_before(name, date=None, days=28):
        s = clean(name)
        if len(s) == 0:
            return np.nan
        end = s.index[-1] if date is None else pd.Timestamp(date)
        old = s[s.index <= end - pd.Timedelta(days=days)]
        return np.nan if len(old) == 0 else float(old.iloc[-1])

    def pct(a, b):
        return np.nan if nan(a) or nan(b) or b == 0 else (a / b - 1) * 100

    def diff(a, b):
        return np.nan if nan(a) or nan(b) else a - b

    def carry_snapshot(date=None):
        usd_now = latest_before('USDJPY', date)
        us2_now = latest_before('Treasury2Y', date)
        us2_4w = value_days_before('Treasury2Y', date, 28)
        vix_now = latest_before('VIX', date)
        nikkei_now = latest_before('Nikkei225', date)
        if 'JapanCall' in df.columns:
            japan_now = latest_before('JapanCall', date)
            japan_4w = value_days_before('JapanCall', date, 28)
        else:
            japan_now = japan_4w = np.nan
        gap_now = np.nan if nan(us2_now) or nan(japan_now) else us2_now - japan_now
        gap_4w = np.nan if nan(us2_4w) or nan(japan_4w) else us2_4w - japan_4w
        return {
            'USDJPY': usd_now,
            'JPY_5D': pct(usd_now, value_days_before('USDJPY', date, 7)),
            'JPY_4W': pct(usd_now, value_days_before('USDJPY', date, 28)),
            'US2Y': us2_now,
            'US2Y_4W': diff(us2_now, us2_4w),
            'VIX': vix_now,
            'VIX_5D': pct(vix_now, value_days_before('VIX', date, 7)),
            'Nikkei': nikkei_now,
            'Nikkei_5D': pct(nikkei_now, value_days_before('Nikkei225', date, 7)),
            'Sahm': latest_before('Sahm', date),
            'JapanRate': japan_now,
            'CarryGap': gap_now,
            'CarryGapChange': diff(gap_now, gap_4w),
        }

    c_now, c_ref = carry_snapshot(), carry_snapshot(EVENT_DATE)
    ok = lambda v: not nan(v)
    trig = {
        'yen':    bool((ok(c_now['JPY_5D']) and c_now['JPY_5D'] <= -2.5) or (ok(c_now['JPY_4W']) and c_now['JPY_4W'] <= -5.0)),
        'rate':   bool(ok(c_now['US2Y_4W']) and c_now['US2Y_4W'] <= -0.30),
        'gap':    bool(ok(c_now['CarryGapChange']) and c_now['CarryGapChange'] <= -0.25),
        'vol':    bool((ok(c_now['VIX']) and c_now['VIX'] >= 25) or (ok(c_now['VIX_5D']) and c_now['VIX_5D'] >= 50)),
        'nikkei': bool(ok(c_now['Nikkei_5D']) and c_now['Nikkei_5D'] <= -7),
        'growth': bool(ok(c_now['Sahm']) and c_now['Sahm'] >= 0.40),
    }
    carry_score = int(sum(trig.values()))
    narrow = trig['rate'] or trig['gap']
    if not trig['yen']:
        carry_regime = 'low'
    elif narrow and trig['vol'] and trig['nikkei']:
        carry_regime = 'very_high'
    elif narrow and (trig['vol'] or trig['nikkei']):
        carry_regime = 'high'
    elif narrow:
        carry_regime = 'rising'
    else:
        carry_regime = 'watch'

    def stress(value, reverse=False, baseline=0.0):
        if nan(value):
            return 0.0
        return max(0.0, -(value - baseline)) if reverse else max(0.0, value - baseline)

    def stress_of(c):
        return {
            'JPY': stress(c['JPY_4W'], reverse=True),
            'US2Y': stress(c['US2Y_4W'], reverse=True),
            'VIX': stress(c['VIX'], baseline=15.0),
            'Nikkei': stress(c['Nikkei_5D'], reverse=True),
            'Sahm': stress(c['Sahm']),
        }

    def ratio(cur, ref):
        if nan(ref) or ref <= 0 or nan(cur):
            return 0.0
        return min(max(cur / ref, 0.0), 1.0)

    s_now, s_ref = stress_of(c_now), stress_of(c_ref)
    parts = {k: ratio(s_now[k], s_ref[k]) for k in CARRY_WEIGHTS}
    similarity = sum(parts[k] * w for k, w in CARRY_WEIGHTS.items()) * 100
    sim_view = 'very' if similarity >= 75 else 'rising' if similarity >= 50 else 'some' if similarity >= 25 else 'low'

    last_of = lambda name: clean(name).index[-1].date().isoformat() if name in df.columns and len(clean(name)) else None
    return {
        'week_date': weekly.index[-1].date().isoformat(),       # 원본 '기준일'(금요일 끝 주 이름 — 이번 주면 앞날일 수 있다)
        'data_date': max(d for d in (last_of(n) for n in ('Treasury2Y', 'Treasury10Y', 'Real10Y', 'RRP')) if d),
        'asof': {n: last_of(n) for n in SERIES},
        'now': {
            'real10y': _num(now['Real10Y']), 'y2': _num(now['Treasury2Y']), 'y10': _num(now['Treasury10Y']),
            'y30': _num(now['Treasury30Y']), 'sp10_2': _num(now['Spread10_2']), 'sp30_10': _num(now['Spread30_10']),
            'fed': _num(now['FedAssets_T']), 'tga': _num(now['TGA_T']), 'rrp': _num(now['RRP_T'], 5),
            'netliq': _num(now['NetLiquidity']), 'loans': _num(now['BankLoans_T']),
            'reserves': _num(now['Reserves_T']), 'm2': _num(m2_now), 'm2_date': m2_date.date().isoformat(),
        },
        'chg': {
            'real10y': _num(real_change), 'y2': _num(y2_change), 'y10': _num(y10_change), 'y30': _num(y30_change),
            'sp10_2': _num(spread10_2_change), 'sp30_10': _num(spread30_10_change), 'netliq': _num(liq_change),
            'loan_4w_pct': _num(bankloan_4w_pct), 'loan_yoy': _num(bankloan_yoy),
            'reserve_4w': _num(reserve_4w_change), 'reserve_4w_pct': _num(reserve_4w_pct),
            'm2_3m_ann': _num(m2_3m_ann), 'm2_yoy': _num(m2_yoy),
        },
        'score': int(score),
        'sig': {'real': sig_real, 'y2': sig_y2, 'steep': sig_steep, 'liq': sig_liq},
        'credit': int(credit_score),
        'credit_sig': {'loan': sig_loan, 'm2': sig_m2},
        'reserve': reserve, 'rrp': rrp, 'total': total, 'macro': macro,
        'long_rate': long_rate, 'rate_struct': rate_struct,
        'gold': gold, 'btc': btc, 'small': small,
        'carry': {
            'now': {k: _num(v) for k, v in c_now.items()},
            'ref': {k: _num(v) for k, v in c_ref.items()},
            'trig': trig, 'score': carry_score, 'regime': carry_regime,
            'similarity': _num(similarity, 2), 'sim_view': sim_view,
            'parts': {k: _num(v) for k, v in parts.items()},
        },
    }


def run(dry: bool = False) -> dict:
    sb = get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[미국유동성] {TABLE} 테이블 없음 — sql/us_indicators.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    p = compute(fetch_all())
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
    if not dry:
        sb.table(TABLE).upsert({'base_date': today, 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()},
                               on_conflict='base_date').execute()
    log.info(f"[미국유동성] {'dry-run ' if dry else ''}{today} 자료 {p['data_date']} · Liquidity {p['score']}/4 · "
             f"Credit {p['credit']}/2 · 종합 {p['total']} · 엔캐리 {p['carry']['score']}/6 {p['carry']['regime']} · "
             f"유사도 {p['carry']['similarity']}%")
    return {'date': today, 'data_date': p['data_date'], 'score': p['score'], 'credit': p['credit'], 'payload': p}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    r = run(dry=ap.parse_args().dry)
    print(json.dumps(r.get('payload', r), ensure_ascii=False, indent=1))
