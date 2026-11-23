"""
collect_us_inverse.py — 미국 인버스 혼용 전략(QQQ + TIP 6개월 월말 리밸런싱 최종 연구안) → market_snapshot(kind='us_inverse')

[원본] 태린이아빠 Market Dashboard '미국 인버스 혼용 전략'(공개 소스 app.py US_INVERSE_MIXED_SRC, v11.60~62 · 11-21 신설)
  · 매달 마지막 거래일 종가로만 판단하고 다음 달 목표 비중을 고정한다(월중 리밸런싱 없음).
  · 상승 = QQQ와 TIP 월말 종가가 각각 월봉 6개월 이동평균 위(둘 중 하나라도 6개월선 자료가 없으면 현금 대기).
      QQQ 최근 3개월 수익 > 0 → QLD 20% 적격 / SOXX 월말 종가 > SOXX 6개월선 → SOXX 35% 적격
      둘 다 적격 QQQ45/QLD20/SOXX35 · SOXX만 탈락 QQQ80/QLD20 · QLD만 탈락 QQQ60/SOXX35/현금5 · 둘 다 탈락 QQQ60/현금40
      최우선 안전핀: QQQ 20거래일 연율 변동성 ≥ 과거 3년(최근 63일 뺀 756일, 252일 미만이면 안 씀) 중앙값 × 2 → QQQ50/현금50
  · 하락 = 그 밖: SPMO50/PSQ50. QQQ 252일 고점 대비 ≤ −30% ∧ 20일 변동성이 직전 월말보다 낮으면 SPMO50/현금50(PSQ 뺌).
  · 원본(기존) 전략: 상승 QQQ60/QLD15/SOXX25, 하락 SPMO50/PSQ50 — 비교용.
  · 백테스트: 신호월 말 종가를 다음 달 출발점으로, 완료된 달만. 편도 0.1% × 0.5 × L1(현금 포함, 직전 달 말 드리프트 비중 대비).
      성과 = CAGR(개월 ÷ 12) · 월말 MDD · 연율 변동성(월 표준편차 × √12) · 누적 배수 · 월 승률, 최종 전략은 일별 평가 MDD도.
  · 최신 달이 지금(뉴욕) 달이면 '다음 달 임시 예비 신호'로만 쓰고 성과에서 뺀다.
[자료] 야후 수정 종가(auto_adjust=True) TIP · QQQ · QLD · SOXX · SPMO · PSQ, 원본 HTS 엑셀과 같은 2020-12-02부터(1,500거래일).
  원본은 필수 ETF에 빈 값이 있으면 멈춘다 — 여기서도 중간 빈 값이면 멈추고, 마지막 날만 일부가 비면(야후 늦음) 그날을 버린다.
[돌리는 때] 매일 아침(미국 장 마감 뒤) 두 번 — 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다.
"""
import argparse
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone

from db_client import get_supabase_client

log = logging.getLogger(__name__)

TABLE = 'market_snapshot'
KIND = 'us_inverse'
KEEP_DAYS = 10
DATA_START = '2020-12-02'      # 원본 HTS 엑셀 시작일(1,500거래일)

# ── 원본 0. 입력 설정 ───────────────────────────────────────────────────
TX_COST = 0.001
VOL_LOOKBACK = 20
VOL_HISTORY_DAYS = 756
VOL_EXCLUDE_RECENT = 63
VOL_MULTIPLIER = 2.0
VOL_MIN_HISTORY = 252
MDD_LOOKBACK = 252
DOWN_PSQ_DD_TRIGGER = -0.30
ETF = ['QQQ', 'QLD', 'SOXX', 'SPMO', 'PSQ']
ASSETS = ETF + ['현금']
REQUIRED = ['TIP'] + ETF
HISTORY_ROWS = 13


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


def _ny_now(now_utc=None):
    now = now_utc or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo('America/New_York'))
    except Exception:            # tzdata 없는 환경(윈도 시험) — 동부 표준시로 근사
        return (now - timedelta(hours=5)).replace(tzinfo=None)


def fetch_prices(start=DATA_START, now_utc=None):
    """야후 수정 종가 6개 → DataFrame(날짜 × 티커). 미국 장중이면 오늘 봉은 버리고, 마지막 날만 일부가 비면 그날을 버린다."""
    import pandas as pd
    import yfinance as yf

    raw = yf.download(REQUIRED, start=start, auto_adjust=True, actions=False, progress=False, threads=False, group_by='column')
    if raw is None or raw.empty:
        raise RuntimeError('야후 일봉을 받지 못했습니다')
    px = raw['Close'].copy()
    px.index = pd.DatetimeIndex(px.index).tz_localize(None).normalize()
    px = px.sort_index().dropna(how='all')
    et = _ny_now(now_utc)
    if len(px) and px.index[-1].date() == et.date() and (et.hour, et.minute) < (16, 15):
        px = px.iloc[:-1]
    missing = [t for t in REQUIRED if t not in px.columns]
    if missing:
        raise RuntimeError(f'야후에서 못 받은 ETF: {missing}')
    px = px[REQUIRED]
    while len(px) and px.iloc[-1].isna().any():   # 야후가 늦게 채우는 마지막 날
        px = px.iloc[:-1]
    return px


def compute(prices, now_utc=None) -> dict:
    """prices: 날짜 × [TIP, QQQ, QLD, SOXX, SPMO, PSQ] 종가. 원본 코드와 같은 순서 · 같은 식."""
    import numpy as np
    import pandas as pd

    prices = prices[REQUIRED].copy()
    prices[prices <= 0] = np.nan
    if prices.isna().any().any():
        bad = prices[prices.isna().any(axis=1)]
        raise RuntimeError(f'필수 ETF 중 빈 시세가 있습니다(보정 없이 멈춤): {[d.date().isoformat() for d in bad.index[:8]]}')
    if len(prices) < 280:
        raise RuntimeError('6개월 추세 · 252일 낙폭 판단을 위한 자료가 부족합니다')

    # 각 달 마지막 실제 거래일 종가
    monthly = prices.groupby(prices.index.to_period('M')).tail(1)
    signal_days = {x.to_period('M'): x for x in monthly.index}
    monthly.index = monthly.index.to_period('M')
    monthly = monthly[~monthly.index.duplicated(keep='last')]
    today_ny = _ny_now(now_utc)
    cur_period = pd.Timestamp(today_ny.date()).to_period('M')
    latest_month = monthly.index[-1]
    provisional = latest_month >= cur_period
    complete = monthly.index[:-1] if provisional else monthly.index
    if len(complete) < 12:
        raise RuntimeError('완료된 달 자료가 부족합니다')

    # 1. 추세 · 안전핀 지표
    ma6_qqq = monthly['QQQ'].rolling(6, min_periods=6).mean()
    ma6_tip = monthly['TIP'].rolling(6, min_periods=6).mean()
    ma6_soxx = monthly['SOXX'].rolling(6, min_periods=6).mean()
    qqq_3m = monthly['QQQ'].pct_change(periods=3, fill_method=None)
    vol20 = prices['QQQ'].pct_change(fill_method=None).rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).std() * np.sqrt(252)
    vol_med = vol20.shift(VOL_EXCLUDE_RECENT).rolling(VOL_HISTORY_DAYS, min_periods=VOL_MIN_HISTORY).median()
    vol_ratio = vol20 / vol_med.replace(0, np.nan)
    dd252 = prices['QQQ'] / prices['QQQ'].rolling(MDD_LOOKBACK, min_periods=MDD_LOOKBACK).max() - 1

    f = lambda s, d: float(s.loc[d]) if d is not None and pd.notna(s.loc[d]) else np.nan
    sig = {}
    for m in monthly.index:
        sd = signal_days[m]
        prev_sd = signal_days.get(m - 1)
        valid = bool(pd.notna(ma6_qqq.loc[m]) and pd.notna(ma6_tip.loc[m]))
        up_qqq = bool(monthly.loc[m, 'QQQ'] > ma6_qqq.loc[m]) if pd.notna(ma6_qqq.loc[m]) else False
        up_tip = bool(monthly.loc[m, 'TIP'] > ma6_tip.loc[m]) if pd.notna(ma6_tip.loc[m]) else False
        up = bool(valid and up_qqq and up_tip)
        v, vm, vr, dd, pv = f(vol20, sd), f(vol_med, sd), f(vol_ratio, sd), f(dd252, sd), f(vol20, prev_sd)
        falling = bool(pd.notna(v) and pd.notna(pv) and v < pv)
        sig[m] = {
            'm': m, 'sd': sd, 'valid': valid, 'up': up, 'up_qqq': up_qqq, 'up_tip': up_tip,
            'qqq': float(monthly.loc[m, 'QQQ']), 'tip': float(monthly.loc[m, 'TIP']), 'soxx': float(monthly.loc[m, 'SOXX']),
            'ma6_qqq': ma6_qqq.loc[m], 'ma6_tip': ma6_tip.loc[m], 'ma6_soxx': ma6_soxx.loc[m], 'qqq_3m': qqq_3m.loc[m],
            'qld_ok': bool(pd.notna(qqq_3m.loc[m]) and qqq_3m.loc[m] > 0),
            'soxx_ok': bool(pd.notna(ma6_soxx.loc[m]) and monthly.loc[m, 'SOXX'] > ma6_soxx.loc[m]),
            'vol': v, 'vol_med': vm, 'vol_ratio': vr, 'prev_vol': pv, 'vol_falling': falling, 'dd': dd,
            'safety': bool(up and pd.notna(vr) and vr >= VOL_MULTIPLIER),
            'psq_off': bool(valid and not up and pd.notna(dd) and dd <= DOWN_PSQ_DD_TRIGGER and falling),
            'done': m in set(complete),
        }

    # 2. 목표 포지션(최종 / 원본)
    def allocation(s, kind='final'):
        w = dict.fromkeys(ASSETS, 0.0)
        if not s['valid']:
            w['현금'] = 1.0
            return w, '추세 지표 준비 부족: 현금 대기'
        if kind == 'original':
            if s['up']:
                w.update(QQQ=0.60, QLD=0.15, SOXX=0.25)
                return w, '원본 상승: QQQ60/QLD15/SOXX25'
            w.update(SPMO=0.50, PSQ=0.50)
            return w, '원본 하락: SPMO50/PSQ50'
        if s['up']:
            if s['safety']:
                w.update(QQQ=0.50, 현금=0.50)
                return w, '상승·변동성 2배 충격: QLD/SOXX 중단, QQQ50/현금50'
            if s['qld_ok'] and s['soxx_ok']:
                w.update(QQQ=0.45, QLD=0.20, SOXX=0.35)
                return w, '상승·QLD/SOXX 모두 적격: QQQ45/QLD20/SOXX35'
            if s['qld_ok'] and not s['soxx_ok']:
                w.update(QQQ=0.80, QLD=0.20)
                return w, '상승·SOXX 탈락·QQQ 3M 플러스: 미편입 잔여 현금을 QQQ로 대체'
            if not s['qld_ok'] and s['soxx_ok']:
                w.update(QQQ=0.60, SOXX=0.35, 현금=0.05)
                return w, '상승·QLD 탈락·SOXX 적격: QQQ60/SOXX35/현금5'
            w.update(QQQ=0.60, 현금=0.40)
            return w, '상승·QLD/SOXX 모두 탈락: QQQ60/현금40'
        if s['psq_off']:
            w.update(SPMO=0.50, 현금=0.50)
            return w, '하락·QQQ DD -30% 이하 + 전월 대비 변동성 하락: PSQ 제거'
        w.update(SPMO=0.50, PSQ=0.50)
        return w, '하락·깊은 DD/변동성 하락 조건 미충족: SPMO50/PSQ50'

    wf, wo, why = {}, {}, {}
    for m, s in sig.items():
        wf[m], why[m] = allocation(s, 'final')
        wo[m], _ = allocation(s, 'original')
        if abs(sum(wf[m].values()) - 1) > 1e-9 or abs(sum(wo[m].values()) - 1) > 1e-9:
            raise RuntimeError(f'월별 비중 합계 오류: {m}')

    # 3. 백테스트 — 완료된 달만, 다음 달 수익 적용
    mret = monthly[ETF].pct_change(fill_method=None)
    cset = set(complete)
    tests = [m for m in monthly.index if m in cset and (m - 1) in sig and sig[m - 1]['valid'] and mret.loc[m].notna().all()]
    if len(tests) < 12:
        raise RuntimeError('백테스트에 필요한 완료 달이 부족합니다')
    if any(tests[j] != tests[j - 1] + 1 for j in range(1, len(tests))):
        raise RuntimeError('월별 시계열이 이어지지 않습니다')

    def backtest(wmap, daily=False):
        eq = 1.0
        prev = dict.fromkeys(ASSETS, 0.0)
        prev['현금'] = 1.0
        rows, mtm = [], []
        for mo in tests:
            w = dict(wmap[mo - 1])
            turnover = 0.5 * sum(abs(w[a] - prev[a]) for a in ASSETS)
            fee = turnover * TX_COST
            start_eq = eq * (1 - fee)
            r = {a: float(mret.loc[mo, a]) for a in ETF}
            gross = 1.0 + sum(w[a] * r[a] for a in ETF)
            if gross <= 0:
                raise RuntimeError(f'ETF 월간 수익 · 비중 오류: {mo}')
            eq = start_eq * gross
            rows.append({'m': mo, 'ret': (1 - fee) * gross - 1.0, 'eq': eq, 'to': turnover})
            if daily:
                anchor = prices.loc[sig[mo - 1]['sd'], ETF]
                sub = prices.loc[prices.index.to_period('M') == mo, ETF]
                for d, row in sub.iterrows():
                    mtm.append((d, start_eq * (w['현금'] + sum(w[a] * float(row[a] / anchor[a]) for a in ETF))))
            prev = {a: w[a] * (1 + r[a]) / gross for a in ETF}
            prev['현금'] = w['현금'] / gross
        return rows, mtm

    bt_f, daily_f = backtest(wf, daily=True)
    bt_o, _ = backtest(wo)
    q_ret = [float(mret.loc[mo, 'QQQ']) for mo in tests]
    q_eq = list(np.cumprod([1 + x for x in q_ret]))

    def metrics(rets, curve, daily=None):
        n = len(rets)
        years = n / 12
        vals = np.r_[1.0, np.asarray(curve, dtype=float)]
        out = {'cagr': float(curve[-1]) ** (1 / years) - 1, 'mdd': float((vals / np.maximum.accumulate(vals) - 1).min()),
               'vol': float(pd.Series(rets).std(ddof=1) * np.sqrt(12)), 'mult': float(curve[-1]),
               'win': float((pd.Series(rets) > 0).mean())}
        if daily:
            vd = np.r_[1.0, np.asarray([x[1] for x in daily], dtype=float)]
            out['dmdd'] = float((vd / np.maximum.accumulate(vd) - 1).min())
        return {k: _num(v, 4) for k, v in out.items()}

    perf = {'final': metrics([r['ret'] for r in bt_f], [r['eq'] for r in bt_f], daily_f),
            'orig': metrics([r['ret'] for r in bt_o], [r['eq'] for r in bt_o]),
            'qqq': metrics(q_ret, q_eq)}

    # 연도별(첫해 · 마지막해는 부분 기간)
    yr = pd.DataFrame({'final': [r['ret'] for r in bt_f], 'orig': [r['ret'] for r in bt_o], 'qqq': q_ret},
                      index=[mo.year for mo in tests])
    yr = (1 + yr).groupby(level=0).prod() - 1

    # 5. 확정 / 임시 포지션
    confirmed = complete[-1]
    preview = latest_month

    def sig_out(m):
        s = sig[m]
        return {'m': str(m), 'apply': str(m + 1), 'sd': s['sd'].date().isoformat(), 'done': s['done'], 'valid': s['valid'],
                'up': s['up'], 'up_qqq': s['up_qqq'], 'up_tip': s['up_tip'],
                'qqq': _num(s['qqq'], 2), 'tip': _num(s['tip'], 2), 'soxx': _num(s['soxx'], 2),
                'ma6_qqq': _num(s['ma6_qqq'], 2), 'ma6_tip': _num(s['ma6_tip'], 2), 'ma6_soxx': _num(s['ma6_soxx'], 2),
                'qqq_3m': _num(s['qqq_3m']), 'qld_ok': s['qld_ok'], 'soxx_ok': s['soxx_ok'],
                'vol': _num(s['vol']), 'vol_med': _num(s['vol_med']), 'vol_ratio': _num(s['vol_ratio'], 3),
                'prev_vol': _num(s['prev_vol']), 'vol_falling': s['vol_falling'], 'dd': _num(s['dd']),
                'safety': s['safety'], 'psq_off': s['psq_off'],
                'w': {a: _num(wf[m][a], 4) for a in ASSETS}, 'w_orig': {a: _num(wo[m][a], 4) for a in ASSETS}, 'why': why[m]}

    drift = None
    if provisional and latest_month == confirmed + 1:
        last_end = sig[confirmed]['sd']
        growth = prices.iloc[-1][ETF] / prices.loc[last_end, ETF]
        vals = {a: float(wf[confirmed][a] * growth[a]) for a in ETF}
        vals['현금'] = float(wf[confirmed]['현금'])
        tot = sum(vals.values())
        drift = {'from': last_end.date().isoformat(), 'to': prices.index[-1].date().isoformat(),
                 'w': {a: _num(v / tot, 4) for a, v in vals.items()}}

    hist = [m for m in monthly.index if sig[m]['valid'] and (m in cset or (provisional and m == latest_month))]
    anchor_sd = sig[tests[0] - 1]['sd']
    qd = prices.loc[anchor_sd:, 'QQQ'] / float(prices.loc[anchor_sd, 'QQQ'])
    d_dates = [d for d, _ in daily_f]
    return {
        'asof': prices.index[-1].date().isoformat(), 'start': prices.index[0].date().isoformat(), 'n_days': int(len(prices)),
        'provisional': bool(provisional), 'confirmed_month': str(confirmed), 'latest_month': str(latest_month),
        'eval': {'from': str(tests[0]), 'to': str(tests[-1]), 'months': len(tests)},
        'confirmed': sig_out(confirmed), 'preview': sig_out(preview) if provisional else None, 'drift': drift,
        'perf': perf,
        'yearly': {'years': [int(y) for y in yr.index], **{k: [_num(v, 4) for v in yr[k]] for k in ('final', 'orig', 'qqq')}},
        'monthly': {'m': [str(mo) for mo in tests], 'final': [_num(r['eq'], 4) for r in bt_f], 'orig': [_num(r['eq'], 4) for r in bt_o],
                    'qqq': [_num(v, 4) for v in q_eq], 'ret': [_num(r['ret'], 4) for r in bt_f]},
        'daily': {'dates': [d.date().isoformat() for d in d_dates], 'final': [_num(v, 4) for _, v in daily_f],
                  'qqq': [_num(qd.loc[d], 4) for d in d_dates]},
        'history': [{'sd': sig[m]['sd'].date().isoformat(), 'apply': str(m + 1), 'done': sig[m]['done'], 'up': sig[m]['up'],
                     'w': {a: _num(wf[m][a], 4) for a in ASSETS}, 'vol_ratio': _num(sig[m]['vol_ratio'], 3),
                     'dd': _num(sig[m]['dd']), 'why': why[m]} for m in hist[-HISTORY_ROWS:]],
    }


def run(dry: bool = False) -> dict:
    sb = None if dry else get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[미국인버스] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    px = fetch_prices()
    p = compute(px)
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': p['asof'], 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
        cutoff = (date.fromisoformat(p['asof']) - timedelta(days=KEEP_DAYS)).isoformat()
        sb.table(TABLE).delete().eq('kind', KIND).lt('base_date', cutoff).execute()
    w = lambda s: ' / '.join(f"{a} {x * 100:.0f}%" for a, x in s['w'].items() if x and x > 1e-8)
    c, v = p['confirmed'], p['preview']
    pv = (' · 예상 ' + v['apply'] + ' ' + w(v)) if v else ''
    log.info(f"[미국인버스] {'dry-run ' if dry else ''}{p['asof']} · 확정 {c['apply']} {w(c)}{pv}"
             f" · {p['eval']['from']}~{p['eval']['to']} {p['perf']['final']['mult']}배")
    return {'date': p['asof'], 'payload': p}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    ap.add_argument('--dump', help='계산 결과를 이 경로에 JSON으로 저장')
    a = ap.parse_args()
    r = run(dry=a.dry)
    if a.dump and r.get('payload'):
        with open(a.dump, 'w', encoding='utf-8') as fh:
            json.dump(r['payload'], fh, ensure_ascii=False)
    print({k: v for k, v in r.items() if k != 'payload'})
