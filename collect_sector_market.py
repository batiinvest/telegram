"""업종 쏠림지수 · 업종 브레드스 국면 — WICS 중분류 28개 (시장 전체 지표, 날짜당 1행)

원본 (태린이아빠, 20260918.zip)
  「특정업종 쏠림지수 국내(매일).xlsx」 — WI26 26개 업종
    롱숏   = [등락률 순위 1~5위 업종 수익률 합 − 나머지 합] ÷ 업종 수   (최종!E = AVERAGE(상위칸) − AVERAGE(하위칸),
             각 칸 범위가 업종 수만큼이고 빠진 칸은 0 — 합을 업종 수로 나눈 것과 같다. 순위 기준 BH11=6 → RANK<6)
    지수화 = 전일 + (1 + 롱숏), 1000에서 시작
    업종쏠림지수 = 지수화의 MACD(EMA12 − EMA26) − 시그널(MACD 9일 단순평균)   ← 원본 차트의 선
    상관관계 = 업종별 일간 수익률과 코스피200 동일가중지수 수익률의 30일 상관계수 평균
             ⚠ 원본 시트(코스피30일!AJ=CORREL($F,G))는 기준열이 한 칸 밀려 동일가중지수(E) 대신 첫 업종
             '에너지'(F)와의 상관을 평균한다(재현 시 원본과 ±0.03). 여기서는 의도대로 동일가중지수를 쓴다.
    원저자 설명: 쏠림지수가 오르면 특정 업종으로 자금이 쏠림 / 내리면 여러 업종이 순환하며 오르는 장.
    시장이 빠지는데 일부 업종만 버텨도 오른다 — 주도 업종 위주 상승인지, 하락 위험인지를 같이 본다.
  「업종지수 활용한 추세 및 비추세 전략 v2.1」 — 120일선 위 업종 비율(Breadth)로 국면
    추세 ≥ 60% · 비추세 40~60% · 회복 30~40% · 역추세 < 30%

여기서는 업종을 WICS 중분류 28개(주도 업종 보드와 같은 단위, 09-29 사용자 결정)로 바꿨다.
공식 검증은 원본과 같은 WI26 26개로 돌려 엑셀과 대조했다(verify_wi26).

데이터
  wics_index_daily    — WICS 중분류 지수 종가(wiseindex, term=0 + fromdt로 2021년부터)
  sector_market_daily — 날짜별 롱숏·지수·MACD·시그널·쏠림지수·상관관계·브레드스·국면
  벤치마크 = KIS 코스피200 동일가중지수(iscd 7001, 원본 엑셀 값과 일치)

실행: 매일 18:50 잡(업종 수급 뒤) run() / 최초 python3 collect_sector_market.py --backfill 2021-01-01
"""

import logging
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = logging.getLogger(__name__)

IDX = 'wics_index_daily'
MKT = 'sector_market_daily'
WISE_URL = 'https://www.wiseindex.com/Index/GetIndexPerformanceChart'
WISE_HDR = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.wiseindex.com/Index/Index',
            'X-Requested-With': 'XMLHttpRequest'}
BENCH_ISCD = '7001'      # 코스피200 동일가중지수 (KIS)
TOP_K = 5                # 원본 RANK < 6
CORR_N = 30
BREADTH_MA = 120
REGIMES = ((0.60, 'trend'), (0.40, 'range'), (0.30, 'recover'), (0.0, 'contra'))
WRITE_DAYS = 60          # 매일 다시 쓰는 범위(달력일)


# ── 원자료 ────────────────────────────────────────────────────────────────────

def fetch_wise(code: str, fromdt: str, dt: str | None = None) -> list:
    """wiseindex 일별 종가 [(YYYY-MM-DD, close)] — term=0 + fromdt(YYYYMMDD)로 원하는 날부터."""
    dt = dt or (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y%m%d')
    err = None
    for attempt in range(3):
        try:
            r = requests.get(WISE_URL, params=dict(dt=dt, fromdt=fromdt, term='0', sec_cd=code, ceil_yn=0),
                             headers=WISE_HDR, timeout=20)
            return [(datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(timezone(timedelta(hours=9)))
                     .strftime('%Y-%m-%d'), float(v)) for ms, v, *_ in r.json().get('data') or [] if v is not None]
        except Exception as e:
            err = e
            time.sleep(1 + attempt)
    log.warning(f'[업종쏠림] {code} 지수 조회 실패: {err}')
    return []


def fetch_bench(start: str) -> dict:
    """{YYYY-MM-DD: 코스피200 동일가중 종가}"""
    from collect_fear_greed import _fetch_index
    end = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y%m%d')
    return _fetch_index(BENCH_ISCD, start.replace('-', ''), end)


# ── 계산 (순수 함수) ──────────────────────────────────────────────────────────

def _corr(a: list, b: list) -> float | None:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if not sa or not sb:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def compute(closes: dict, bench: dict) -> list:
    """closes {업종: {date: close}}, bench {date: close} → 날짜별 지표 [{...}] (오름차순)

    원본 시트와 같은 순서로 계산한다. 날짜 = 모든 업종에 종가가 있는 날.
    """
    secs = sorted(closes)
    n = len(secs)
    dates = sorted(set.intersection(*(set(closes[s]) for s in secs)))
    out, idx = [], 1000.0
    ema12 = ema26 = None
    macds, hist = [], []
    rets_hist = {s: [] for s in secs}
    bench_hist = []
    for i in range(1, len(dates)):
        d, p = dates[i], dates[i - 1]
        r = {s: (closes[s][d] / closes[s][p] - 1) * 100 for s in secs}   # 원본 업종수익률(%)
        # RANK(값, 행, 0) — 내림차순, 동점은 같은 순위
        vals = list(r.values())
        rank = {s: 1 + sum(1 for v in vals if v > r[s]) for s in secs}
        top = sum(r[s] for s in secs if rank[s] < TOP_K + 1)
        rest = sum(r[s] for s in secs if rank[s] >= TOP_K + 1)
        spread = (top - rest) / n
        idx = idx + (1 + spread)
        hist.append(idx)
        # MACD — EMA12는 첫 값에서, EMA26은 첫 26개 단순평균에서 시작(원본 H40=AVERAGE(F15:F40))
        ema12 = idx if ema12 is None else idx * 2 / 13 + ema12 * (1 - 2 / 13)
        if len(hist) == 26:
            ema26 = sum(hist) / 26
        elif ema26 is not None:
            ema26 = idx * 2 / 27 + ema26 * (1 - 2 / 27)
        macd = sig = osc = None
        if ema26 is not None:
            macd = ema12 - ema26
            macds.append(macd)
            if len(macds) >= 9:
                sig = sum(macds[-9:]) / 9          # 원본 J = AVERAGE(I 9칸) — 단순평균
                osc = macd - sig
        # 상관관계 — 업종 수익률 vs 코스피200 동일가중 수익률, 30일
        for s in secs:
            rets_hist[s].append(r[s])
        b = (bench[d] / bench[p] - 1) if d in bench and p in bench else None
        bench_hist.append(b)
        corr = None
        if len(bench_hist) >= CORR_N and None not in bench_hist[-CORR_N:]:
            bw = bench_hist[-CORR_N:]
            cs = [c for c in (_corr(bw, rets_hist[s][-CORR_N:]) for s in secs) if c is not None]
            corr = sum(cs) / len(cs) if cs else None
        # 브레드스 — 종가 > 120일 단순평균인 업종 비율
        above = cnt = 0
        if i + 1 >= BREADTH_MA:
            win = dates[i + 1 - BREADTH_MA:i + 1]
            for s in secs:
                ma = sum(closes[s][x] for x in win) / BREADTH_MA
                cnt += 1
                above += closes[s][d] > ma
        breadth = above / cnt if cnt else None
        regime = next((k for th, k in REGIMES if breadth is not None and breadth >= th), None)
        out.append({'base_date': d, 'spread': round(spread, 6), 'spread_idx': round(idx, 4),
                    'macd': None if macd is None else round(macd, 6),
                    'sig': None if sig is None else round(sig, 6),
                    'osc': None if osc is None else round(osc, 6),
                    'corr30': None if corr is None else round(corr, 4),
                    'breadth': None if breadth is None else round(breadth, 4),
                    'n_above': above if cnt else None, 'n_sectors': n, 'regime': regime,
                    'top5': [s for s in sorted(secs, key=lambda x: rank[x])[:TOP_K]]})
    return out


# ── DB ────────────────────────────────────────────────────────────────────────

def _has_schema(sb) -> bool:
    try:
        sb.table(IDX).select('mid_code').limit(1).execute()
        sb.table(MKT).select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def sync_index(sb, start: str | None = None) -> int:
    """WICS 중분류 28개 지수 종가 — 저장된 마지막 날 15일 전부터(또는 start부터) 다시 받는다."""
    from collect_wics import WICS_MIDS
    if not start:
        last = sb.table(IDX).select('base_date').order('base_date', desc=True).limit(1).execute().data
        start = ((date.fromisoformat(last[0]['base_date']) - timedelta(days=15)).isoformat()
                 if last else '2021-01-01')
    fromdt = start.replace('-', '')
    with ThreadPoolExecutor(4) as ex:
        got = dict(zip(WICS_MIDS, ex.map(lambda c: fetch_wise(c, fromdt), list(WICS_MIDS))))
    rows = [{'base_date': d, 'mid_code': c, 'close_idx': v} for c, pts in got.items() for d, v in pts]
    for i in range(0, len(rows), 1000):
        sb.table(IDX).upsert(rows[i:i + 1000], on_conflict='base_date,mid_code').execute()
    empty = [c for c, pts in got.items() if not pts]
    log.info(f"[업종쏠림] WICS 지수 {len(rows)}행 ({start}~)" + (f" · 조회 실패 {empty}" if empty else ''))
    return len(rows)


def compute_all(sb, write_days: int | None = WRITE_DAYS) -> int:
    rows = fetch_all_pages(sb.table(IDX).select('base_date,mid_code,close_idx')
                           .order('base_date').order('mid_code'))
    closes = {}
    for r in rows:
        closes.setdefault(r['mid_code'], {})[r['base_date']] = float(r['close_idx'])
    start = min((min(v) for v in closes.values()), default=None)
    if not start:
        return 0
    out = compute(closes, fetch_bench(start))
    if write_days is not None:
        since = (date.today() - timedelta(days=write_days)).isoformat()
        out = [r for r in out if r['base_date'] >= since]
    for i in range(0, len(out), 1000):
        sb.table(MKT).upsert(out[i:i + 1000], on_conflict='base_date').execute()
    last = out[-1] if out else {}
    log.info(f"[업종쏠림] {len(out)}일 기록 — 최신 {last.get('base_date')} 쏠림 {last.get('osc')} · "
             f"상관 {last.get('corr30')} · 120일선 위 {last.get('n_above')}/{last.get('n_sectors')} ({last.get('regime')})")
    return len(out)


def run() -> int:
    sb = get_supabase_client()
    if not _has_schema(sb):
        log.warning('[업종쏠림] sql/sector_board.sql 미실행 — 건너뜀')
        return 0
    sync_index(sb)
    return compute_all(sb)


def backfill(start: str = '2021-01-01') -> int:
    sb = get_supabase_client()
    sync_index(sb, start)
    return compute_all(sb, write_days=None)


# ── 원본 대조 ─────────────────────────────────────────────────────────────────

WI26 = ('WI100', 'WI110', 'WI200', 'WI210', 'WI220', 'WI230', 'WI240', 'WI250', 'WI260', 'WI300',
        'WI310', 'WI320', 'WI330', 'WI340', 'WI400', 'WI410', 'WI500', 'WI510', 'WI520', 'WI600',
        'WI610', 'WI620', 'WI630', 'WI640', 'WI700', 'WI800')


def verify_wi26(start: str = '2021-11-02') -> list:
    """원본과 같은 WI26 26개로 계산 — 엑셀(최종!K·코스피30일!AI)과 대조용. DB에 쓰지 않는다."""
    fromdt = start.replace('-', '')
    with ThreadPoolExecutor(4) as ex:
        got = dict(zip(WI26, ex.map(lambda c: dict(fetch_wise(c, fromdt)), WI26)))
    return compute(got, fetch_bench(start))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    for _n in ('httpx', 'httpcore', 'hpack', 'urllib3'):
        logging.getLogger(_n).setLevel(logging.WARNING)
    a = sys.argv[1:]
    if '--backfill' in a:
        backfill(a[a.index('--backfill') + 1] if len(a) > a.index('--backfill') + 1 else '2021-01-01')
    elif '--verify' in a:
        import json
        print(json.dumps(verify_wi26()))
    else:
        run()
