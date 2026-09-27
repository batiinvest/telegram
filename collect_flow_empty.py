"""수급 '빈집' 판정 — market_data.flow_quad / flow_pctl 계산

기업분석 표에 '수급빈집' 칩을 붙이기 위한 사전 계산.
판정 정의는 **수급 지도(js/flow-map.js) 빈집 모드와 한 글자도 다르지 않다** — 같은 값을
두 화면이 서로 다르게 말하면 둘 다 못 믿게 되므로, 아래 상수·창·순위 규칙을 바꿀 때는
flow-map.js 쪽도 같이 바꿔야 한다(_FM_OSC_N · _FM_EMPTY_TH · _FM_WINS의 3M · _fmQuadE).

왜 백엔드인가
    빈집은 5일 오실레이터를 자기 이력 63거래일과 비교해야 나오는 값이다. 표는 당일 행만
    읽으므로(2,559행) 화면에서 계산하려면 이력 2.8만 행을 더 받아야 하고, 첫 로딩을
    2.0초 → 1.0초로 줄인 작업과 정면으로 상충한다. 그래서 수급 수집 뒤 여기서 계산해
    당일 행에 적어두고, 표는 컬럼 하나만 읽는다.

커버리지
    수급(foreign_net_buy·institution_net_buy)은 모니터링 종목에만 매일 쌓인다.
    2026-09-27 실측: 수급이 있는 종목 777개 중 이력 40거래일 이상은 311개 = 모니터링 전량.
    나머지 466개는 18:15 랭킹 잡이 거래대금 상위일에만 주워온 것이라 5일 연속 창이
    안 차고, 비교할 동종 집단(테마)도 없다. → 판정은 311종목, 나머지는 NULL이다.
"""

import logging
import statistics
from collections import defaultdict

from db_client import get_supabase_client

log = logging.getLogger(__name__)

# ── 수급 지도와 공유하는 상수 (flow-map.js) ────────────────────────────────────
OSC_N = 5        # _FM_OSC_N — 오실레이터 롤링 창(태린이아빠 원 정의 5거래일 고정)
SPAN = 63        # _FM_WINS의 3M med — 비교 이력 창
EMPTY_TH = 30    # _FM_EMPTY_TH — ★(뚜렷한 빈집) 백분위 기준
MIN_SER = 8      # 백분위를 말할 최소 표본 (_fmRenderEmpty)
LOOKBACK_DAYS = 200   # 63거래일을 담기 위한 달력일 여유

QUADS = ('fill', 'full', 'bnce', 'cold')


def _quad(x: float, y: float) -> str:
    """_fmQuadE와 동일 — 가로=업종 내 공급강도 순위, 세로=자기 이력 대비 현재 위치."""
    if x >= 0:
        return 'fill' if y < 0 else 'full'
    return 'cold' if y < 0 else 'bnce'


def _net(day: dict | None) -> float | None:
    """외국인+기관 순매수 주식수 (FM.inv='both' 기본값과 동일)."""
    if not day:
        return None
    f, i = day['f'], day['i']
    if f is None and i is None:
        return None
    return (f or 0) + (i or 0)


def _osc_series(days: dict, dates: list) -> list:
    """osc(d) = Σ(d-4..d) 순매수대금 ÷ 당일 시가총액 × 100(%)  — _fmOscSeries와 동일.

    분모를 '당일' 시총으로 잡아 기간 중 주가가 크게 변한 종목의 왜곡을 없앤다.
    창이 덜 찬 날(결측 포함)은 버려 5일 합산의 의미를 지킨다.
    """
    out = []
    for i in range(OSC_N - 1, len(dates)):
        di = days.get(dates[i])
        cap = di['cap'] if di else None
        if not cap or cap <= 0:
            continue
        total, hit = 0.0, 0
        for k in range(i - OSC_N + 1, i + 1):
            d = days.get(dates[k])
            n = _net(d)
            if n is None or d['p'] is None:
                continue
            total += n * d['p']
            hit += 1
        if hit < OSC_N:
            continue
        out.append((dates[i], total / cap * 100))
    return out


def _fetch_pages(q, order_cols, page=1000):
    out, off = [], 0
    while True:
        qq = q
        for c in order_cols:
            qq = qq.order(c, desc=False)
        rows = qq.range(off, off + page - 1).execute().data or []
        out += rows
        if len(rows) < page:
            return out
        off += page


def _load(sb, cutoff: str):
    """모니터링+테마 종목의 수급 이력을 테마별로 묶어 돌려준다."""
    co = _fetch_pages(
        sb.table('companies').select('code,industry')
          .eq('active', True).eq('is_monitored', True), ['code'])
    theme_of = {c['code']: c['industry'] for c in co if c['industry']}
    if not theme_of:
        return {}, None

    codes = list(theme_of)
    rows = []
    for i in range(0, len(codes), 300):
        rows += _fetch_pages(
            sb.table('market_data')
              .select('stock_code,base_date,price,market_cap,'
                      'foreign_net_buy,institution_net_buy')
              .in_('stock_code', codes[i:i + 300])
              .gte('base_date', cutoff)
              .not_.is_('foreign_net_buy', 'null'),
            # (base_date, stock_code) 총순서 — PK 기준이라 페이지 경계에서 행이 안 섞인다
            ['base_date', 'stock_code'])

    themes = defaultdict(lambda: {'dates': set(), 'byCode': {}})
    for r in rows:
        t = themes[theme_of[r['stock_code']]]
        t['dates'].add(r['base_date'])
        s = t['byCode'].setdefault(r['stock_code'], {})
        s[r['base_date']] = {
            'f': r['foreign_net_buy'], 'i': r['institution_net_buy'],
            'p': r['price'], 'cap': r['market_cap'],
        }
    for t in themes.values():
        t['dates'] = sorted(t['dates'])
    return themes, max((r['base_date'] for r in rows), default=None)


def classify(sb, cutoff: str):
    """테마별 빈집 판정 → {code: (quad, pctl)} 와 수급 최신일."""
    themes, last_flow = _load(sb, cutoff)
    result, skipped = {}, defaultdict(int)

    for theme, t in themes.items():
        dates = t['dates']
        if len(dates) < OSC_N + 3:
            skipped[f'{theme}:이력부족({len(dates)}일)'] += 1
            continue
        span_n = min(SPAN, len(dates))
        cut_date = dates[max(0, len(dates) - span_n)]

        base = []
        for code, days in t['byCode'].items():
            ser = [o for o in _osc_series(days, dates) if o[0] >= cut_date]
            if len(ser) < MIN_SER:
                skipped[f'{theme}:표본부족'] += 1
                continue
            vals = [v for _, v in ser]
            cur = vals[-1]
            base.append({
                'code': code, 'cur': cur, 'avg': statistics.fmean(vals),
                'pct': sum(1 for v in vals if v < cur) / len(vals) * 100,
            })

        n = len(base)
        for p in base:
            # 가로축 = 테마 내 공급강도 순위. 평균의 절대 부호로 가르면 0 근처에 몰려
            # 좌/우가 잡음으로 갈리므로 순위로 환산한다(수급 지도와 같은 이유).
            sup = (sum(1 for o in base if o['avg'] < p['avg']) / n * 100) if n > 1 else 50
            result[p['code']] = (_quad(sup - 50, p['pct'] - 50), round(p['pct']))

        log.info(f"  {theme:<7} 판정 {n}종목 / 보유 {len(dates)}거래일 "
                 f"(창 {cut_date}~{dates[-1]})")

    if skipped:
        log.info('  제외: ' + ' · '.join(f'{k}×{v}' for k, v in sorted(skipped.items())))
    return result, last_flow


def _write(sb, verdicts: dict, target_date: str) -> int:
    """(quad, pctl) 같은 종목끼리 묶어 일괄 PATCH.

    부분 컬럼 upsert는 못 쓴다 — PostgREST가 INSERT 후보를 먼저 만들어
    market_data.corp_name의 NOT NULL을 충돌 판정보다 먼저 검사해 23502로 실패한다
    (수익률 수집에서 실측). 값이 같은 종목을 묶으면 311종목이 ~120번의 UPDATE로 끝난다.
    """
    groups = defaultdict(list)
    for code, (quad, pctl) in verdicts.items():
        groups[(quad, pctl)].append(code)

    done = 0
    for (quad, pctl), codes in groups.items():
        for i in range(0, len(codes), 200):
            d = sb.table('market_data') \
                  .update({'flow_quad': quad, 'flow_pctl': pctl}) \
                  .eq('base_date', target_date) \
                  .in_('stock_code', codes[i:i + 200]).execute().data
            done += len(d or [])
    log.info(f"[빈집] {target_date} {done}행 기록 ({len(groups)}회 UPDATE)")
    return done


def run(dry: bool = False) -> int:
    """수급 수집 후 실행 — 당일 market_data 행에 빈집 판정을 적는다."""
    from datetime import date, timedelta
    sb = get_supabase_client()
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    log.info('=== [빈집] 수급 빈집 판정 시작 ===')
    if not dry:
        # 컬럼이 없으면 계산을 다 해놓고 쓰기에서 PGRST204로 끊긴다 — 먼저 확인해 원인을 바로 알린다
        try:
            sb.table('market_data').select('flow_quad,flow_pctl').limit(1).execute()
        except Exception as e:
            raise RuntimeError('market_data.flow_quad/flow_pctl 컬럼 없음 — '
                               'sql/flow_empty.sql을 먼저 실행하세요') from e
    verdicts, last_flow = classify(sb, cutoff)
    if not verdicts:
        log.warning('[빈집] 판정 결과 없음 — 수급 이력을 확인하세요')
        return 0

    cnt = defaultdict(int)
    for q, p in verdicts.values():
        cnt[q] += 1
    deep = sum(1 for q, p in verdicts.values() if q == 'fill' and p <= EMPTY_TH)
    log.info(f"[빈집] 판정 {len(verdicts)}종목 — "
             + ' · '.join(f'{q} {cnt[q]}' for q in QUADS)
             + f" (★뚜렷한 빈집 {deep})")

    # 표가 읽는 날짜에 적는다 — 표는 market_data 최신일 행만 조회한다.
    latest = sb.table('market_data').select('base_date') \
               .order('base_date', desc=True).limit(1).execute().data
    target = latest[0]['base_date'] if latest else None
    if not target:
        log.error('[빈집] market_data 최신일을 찾을 수 없음')
        return 0
    if target != last_flow:
        # 수급이 아직 안 들어온 날 — 판정은 last_flow 기준이라 하루 묵는다.
        log.warning(f"[빈집] 수급 최신일 {last_flow} ≠ 표 기준일 {target} "
                    f"— {last_flow} 기준 판정을 {target} 행에 적습니다")

    if dry:
        log.info(f"[빈집] dry-run — {target} 행에 {len(verdicts)}건 기록 예정 (쓰기 생략)")
        return len(verdicts)
    return _write(sb, verdicts, target)


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    run(dry='--dry' in sys.argv)
