"""업종 수급 오실레이터 — WICS 중분류 × 시장(합산·코스피·코스닥)

원본: 태린이아빠 「외국인기관수급오실레이터 (업종)(태린이아빠)(매일).xlsm」 (20260918.zip)
  시기외 = 업종 5일 누적 (외국인+기관) 순매수대금 ÷ 업종 시가총액
  오실   = MACD(EMA 2/13 − EMA 2/27) − 시그널(EMA 2/10), EMA는 첫 값에서 시작
  그래프뽑기 = 기간 안 분위수(상위10·25·평균·하위25·하위10)로 현재 위치를 본다
원본은 DataGuide 업종 지수 수급(WISE 섹터·KRX·테마 69개 혼합)을 쓴다. 여기서는 종목 수급을
WICS로 합친다 — 원본 반도체와반도체장비(코스피 G4530) 칸과 60거래일 대조: 5일 순매수 상관 0.998·
부호 97%·크기 97%, 시가총액은 우선주가 빠져 8% 작다(09-29 실측).

계산은 종목 수급 오실레이터(collect_flow_empty)와 같은 함수(_macd_hist·_stage)를 쓴다 —
MACD 상수를 바꾸면 종목·업종이 함께 바뀐다.

두 표
  sector_flow_daily — 날짜 × WICS 소분류 × 시장 원자료 합계(순매수 금액·시가총액·커버리지).
                      중분류·섹터·WI26 등 어떤 묶음도 여기서 다시 합친다. market_data와 달리
                      보존 제한이 없어 이력이 계속 쌓인다.
  sector_flow_osc   — 날짜 × 중분류 × 시장(ALL·KOSPI·KOSDAQ) 오실레이터·수급 칸·단계.

실행
  매일(18:50 잡, 수급 정산 뒤) run() — 최근 45일을 market_data(재정산된 값)로 다시 합치고
      오실레이터를 다시 계산해 최근 WRITE_DAYS일을 쓴다.
  일회성 backfill(start) — market_data에 전종목 행이 없는 과거(08-23 이전)를 KIS 종목별 일별
      수급(FHPTJ04160001, 30거래일씩 거슬러 감)으로 채운다. 시가총액 = 현재 상장주식수 × 그날 종가.
      python3 collect_sector_flow.py --backfill 2025-09-01 [--rate 8]
      python3 collect_sector_flow.py --from-cache      # 받아 둔 원자료로 다시 쓰기
"""

import json
import logging
import os
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = logging.getLogger(__name__)

DAILY = 'sector_flow_daily'
OSC = 'sector_flow_osc'
MARKETS = ('KOSPI', 'KOSDAQ')
GROUPS = ('ALL',) + MARKETS
SYNC_DAYS = 45        # 매일 market_data로 다시 합치는 범위(달력일) — 수급 정산(30거래일)을 덮는다
WRITE_DAYS = 40       # 매일 다시 쓰는 오실레이터 범위(달력일)
MIN_COVER = 0.9       # 그날 업종 시가총액 중 수급이 있는 비중이 이보다 작으면 그날은 뺀다
FULL_DAY = 0.9        # market_data 한 날의 행 수가 최대의 이 비율 이상이어야 전종목 날로 본다
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seed', 'sector_flow_raw.json')


# ── 원자료 합계 ───────────────────────────────────────────────────────────────

def _companies(sb) -> dict:
    """{code: (wics_code, market)} — WICS 소분류와 시장이 있는 상장사."""
    rows = fetch_all_pages(sb.table('companies').select('code,market,wics_code')
                             .eq('active', True).order('code'))
    return {r['code']: (r['wics_code'], r['market']) for r in rows
            if r.get('wics_code') and r.get('market') in MARKETS}


def _aggregate(items, comp: dict) -> dict:
    """items: (date, code, net_qty|None, price, cap) → {(date, wics_code, market): 합계}"""
    agg = defaultdict(lambda: {'net_amt': 0, 'cap': 0, 'cap_flow': 0, 'n_stocks': 0, 'n_flow': 0})
    for d, code, net, price, cap in items:
        key = comp.get(code)
        if not key or not price or not cap:
            continue
        a = agg[(d, key[0], key[1])]
        a['cap'] += cap
        a['n_stocks'] += 1
        if net is not None:
            a['net_amt'] += net * price
            a['cap_flow'] += cap
            a['n_flow'] += 1
    return agg


def _write_daily(sb, agg: dict) -> int:
    rows = [{'base_date': d, 'wics_code': w, 'market': m,
             **{k: int(round(v)) for k, v in a.items()}}
            for (d, w, m), a in sorted(agg.items())]
    for i in range(0, len(rows), 1000):
        sb.table(DAILY).upsert(rows[i:i + 1000], on_conflict='base_date,wics_code,market').execute()
    return len(rows)


def sync_daily(sb, comp: dict, days: int = SYNC_DAYS) -> int:
    """최근 days일 — market_data(수급 정산을 거친 값)로 소분류 합계를 다시 쓴다."""
    since = (date.today() - timedelta(days=days)).isoformat()
    codes = sorted(comp)

    def page(chunk):
        from collect_flow_empty import _thread_client
        return fetch_all_pages(
            _thread_client().table('market_data')
              .select('stock_code,base_date,price,market_cap,foreign_net_buy,institution_net_buy')
              .in_('stock_code', chunk).gte('base_date', since)
              .order('base_date').order('stock_code'))
    rows = []
    with ThreadPoolExecutor(4) as ex:
        for part in ex.map(page, [codes[i:i + 200] for i in range(0, len(codes), 200)]):
            rows += part
    # 전종목 행이 없던 날(08-23 이전, 모니터링 종목만)은 합계가 업종을 대표하지 못한다 — 백필 몫
    per_day = defaultdict(int)
    for r in rows:
        per_day[r['base_date']] += 1
    top = max(per_day.values(), default=0)
    full = {d for d, n in per_day.items() if n >= top * FULL_DAY}

    def net(r):
        f, i = r['foreign_net_buy'], r['institution_net_buy']
        return None if f is None and i is None else (f or 0) + (i or 0)
    agg = _aggregate(((r['base_date'], r['stock_code'], net(r), r['price'], r['market_cap'])
                      for r in rows if r['base_date'] in full), comp)
    n = _write_daily(sb, agg)
    log.info(f"[업종수급] market_data {len(full)}일 → 소분류 합계 {n}행")
    return n


# ── 백필 (과거 전종목) ─────────────────────────────────────────────────────────

def _shares(sb, codes: list) -> dict:
    """{code: 상장주식수} — 최신 market_data의 시가총액 ÷ 종가."""
    latest = sb.table('market_data').select('base_date').order('base_date', desc=True) \
               .limit(1).execute().data[0]['base_date']
    rows = fetch_all_pages(sb.table('market_data').select('stock_code,price,market_cap')
                             .eq('base_date', latest).order('stock_code'))
    return {r['stock_code']: r['market_cap'] / r['price'] for r in rows
            if r.get('price') and r.get('market_cap') and r['stock_code'] in codes}


def backfill(start: str, rate: int = 8, workers: int = 6) -> int:
    """start(YYYY-MM-DD)부터 어제까지 — KIS 종목별 일별 수급으로 소분류 합계를 채운다.

    호출 약 (거래일/30)×종목 수. 봇과 KIS 한도(초당 15)를 나누도록 이 프로세스는 rate로 낮춘다.
    받은 원자료는 CACHE에 먼저 저장한다 — 표가 없거나 쓰기에 실패해도 --from-cache로 다시 쓴다.
    """
    from collect_flow_empty import _fetch_daily_raw
    from managers import kis_rate_limiter, safe_int
    kis_rate_limiter.max_calls = rate
    sb = get_supabase_client()
    comp = _companies(sb)
    shares = _shares(sb, set(comp))
    codes = sorted(c for c in comp if c in shares)
    t0 = time.time()

    def walk(code):
        got, anchor = {}, date.today() - timedelta(days=1)
        for _ in range(20):   # 안전장치 — 1년이면 9회 안팎
            part = _fetch_daily_raw(code, anchor.strftime('%Y%m%d'))
            if not part:
                break
            for d, r in part.items():
                if d < start:
                    continue
                f = str(r.get('frgn_ntby_qty') or '').strip()
                o = str(r.get('orgn_ntby_qty') or '').strip()
                px = safe_int(r.get('stck_clpr'))
                if px:
                    got[d] = [None if not (f or o) else safe_int(f) + safe_int(o), px]
            oldest = min(part)
            if oldest <= start:
                break
            anchor = datetime.strptime(oldest, '%Y-%m-%d').date() - timedelta(days=1)
        return code, got

    raw, failed = {}, 0
    with ThreadPoolExecutor(workers) as ex:
        for n, (code, got) in enumerate(ex.map(walk, codes), 1):
            if got:
                raw[code] = got
            else:
                failed += 1
            if n % 250 == 0:
                log.info(f"[업종수급 백필] 조회 {n}/{len(codes)} ({time.time() - t0:.0f}초)")
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w', encoding='utf-8') as f:
        json.dump({'start': start, 'shares': {c: shares[c] for c in raw}, 'raw': raw}, f)
    log.info(f"[업종수급 백필] 조회 완료 {len(raw)}종목(실패 {failed}) · {time.time() - t0:.0f}초 · 캐시 {CACHE}")
    if not _has_schema(sb):
        log.warning('[업종수급 백필] sql/sector_flow.sql 미실행 — 캐시만 저장. 실행 후 --from-cache')
        return 0
    return write_cache(sb, comp)


def write_cache(sb=None, comp: dict | None = None) -> int:
    """CACHE 원자료 → 소분류 합계 쓰기 → 최근분은 market_data로 덮기 → 오실레이터 전체 계산."""
    sb = sb or get_supabase_client()
    comp = comp or _companies(sb)
    with open(CACHE, encoding='utf-8') as f:
        cache = json.load(f)
    sh = cache['shares']
    items = ((d, code, v[0], v[1], sh[code] * v[1])
             for code, days in cache['raw'].items() for d, v in days.items())
    n = _write_daily(sb, _aggregate(items, comp))
    log.info(f"[업종수급 백필] 소분류 합계 {n}행 기록")
    sync_daily(sb, comp)                       # 정산된 market_data 값이 있는 최근분은 그것으로
    compute_osc(sb, write_days=None)
    return n


# ── 오실레이터 ────────────────────────────────────────────────────────────────

def _series(rows: list) -> tuple:
    """소분류 행 → ({(mid, group): {date: [net, cap_flow, cap]}}, 거래일 목록)"""
    acc = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    dates = set()
    for r in rows:
        mid = r['wics_code'][:5]
        dates.add(r['base_date'])
        for g in ('ALL', r['market']):
            a = acc[(mid, g)][r['base_date']]
            a[0] += r['net_amt'] or 0
            a[1] += r['cap_flow'] or 0
            a[2] += r['cap'] or 0
    return acc, sorted(dates)


def _x_series(days: dict, dates: list) -> list:
    """5일 비율 x(d) = Σ(d-4..d) 순매수 금액 ÷ 그날 수급 있는 시가총액 × 100(%) — 5일 모두 유효해야."""
    from collect_flow_empty import OSC_N
    ok = {d for d, (net, capf, cap) in days.items() if cap and capf / cap >= MIN_COVER}
    out = []
    for i in range(OSC_N - 1, len(dates)):
        win = dates[i - OSC_N + 1:i + 1]
        if all(d in ok for d in win):
            out.append((dates[i], sum(days[d][0] for d in win) / days[dates[i]][1] * 100))
    return out


def _gauge(vals: list) -> dict:
    """수급 칸 — collect_flow_empty._stat과 같은 분위수(Excel PERCENTILE.INC)."""
    q10 = statistics.quantiles(vals, n=10, method='inclusive')
    q4 = statistics.quantiles(vals, n=4, method='inclusive')
    cur = vals[-1]
    return {'lv': [round(v, 6) for v in (q10[8], q4[2], statistics.fmean(vals), q4[0], q10[0])],
            'cur': round(cur, 6), 'prev': round(vals[-2], 6)}


def compute_osc(sb, write_days: int | None = WRITE_DAYS) -> int:
    """소분류 합계 전체 → 중분류 × 시장 오실레이터·수급 칸·단계. write_days=None이면 전체를 쓴다."""
    from collect_flow_empty import _macd_hist, _stage, OSC_WARMUP, SPAN, MIN_SER
    rows = fetch_all_pages(sb.table(DAILY)
                           .select('base_date,wics_code,market,net_amt,cap,cap_flow')
                           .order('base_date').order('wics_code').order('market'))
    names = {}
    for c in fetch_all_pages(sb.table('companies').select('wics_code,wics_mid').eq('active', True)
                             .not_.is_('wics_code', 'null').order('code')):
        if c.get('wics_mid'):
            names.setdefault(c['wics_code'][:5], c['wics_mid'])
    acc, dates = _series(rows)
    since = None if write_days is None else (date.today() - timedelta(days=write_days)).isoformat()

    out = []
    for (mid, g), days in acc.items():
        xs = _x_series(days, dates)
        osc = _macd_hist(xs)
        xv = dict(xs)
        hist = []                                   # 워밍업 뒤 오실레이터 이력
        for i, (d, o) in enumerate(osc):
            if i < OSC_WARMUP:
                continue
            hist.append(o)
            if since and d < since:
                continue
            win = hist[-SPAN:]
            gauge = _gauge(win) if len(win) >= MIN_SER else None
            net, capf, cap = days[d]
            out.append({'base_date': d, 'mid_code': mid, 'market': g, 'name': names.get(mid),
                        'x': round(xv[d], 6), 'osc': round(o, 7),
                        'pct': round(sum(1 for v in win if v < o) / len(win) * 100, 1) if gauge else None,
                        'gauge': gauge, 'stage': _stage(gauge),
                        'cover': round(capf / cap, 4) if cap else None})
    for i in range(0, len(out), 1000):
        sb.table(OSC).upsert(out[i:i + 1000], on_conflict='base_date,mid_code,market').execute()
    last = max((r['base_date'] for r in out), default=None)
    log.info(f"[업종수급] 오실레이터 {len(out)}행 기록 (최신 {last})")
    return len(out)


def _has_schema(sb) -> bool:
    try:
        sb.table(OSC).select('mid_code').limit(1).execute()
        sb.table(DAILY).select('wics_code').limit(1).execute()
        return True
    except Exception:
        return False


def run() -> int:
    """매일 — 수급 정산 뒤. 최근분 합계를 다시 쓰고 오실레이터를 다시 계산한다."""
    sb = get_supabase_client()
    if not _has_schema(sb):
        log.warning('[업종수급] sql/sector_flow.sql 미실행 — 건너뜀')
        return 0
    sync_daily(sb, _companies(sb))
    return compute_osc(sb)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    for _n in ('httpx', 'httpcore', 'hpack', 'urllib3'):
        logging.getLogger(_n).setLevel(logging.WARNING)
    args = sys.argv[1:]
    if '--backfill' in args:
        rate = int(args[args.index('--rate') + 1]) if '--rate' in args else 8
        backfill(args[args.index('--backfill') + 1], rate=rate)
    elif '--from-cache' in args:
        write_cache()
    else:
        run()
