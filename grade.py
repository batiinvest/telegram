"""
grade.py — 실적 등급 계산 & earnings_grade_history 저장

등급 기준:
  S  : 매출 YoY 30%↑ + 영업이익 YoY 20%↑ + 이익률 2%p↑ + 전분기 YoY 20%↑ (둘 중 하나만이면 A)
  A  : 매출 YoY 30%↑ + (흑자전환 or 영업이익 YoY 0%↑)
  B  : 매출 YoY 20%↑ + 영업이익 흑자
  관찰: 매출 QoQ 20%↑ + (영업이익 흑자전환 or 2분기 연속 적자 축소 or 매출 하락 후 반등)

제외 조건:
  - 매출 50억 미만
  - 베이스효과 (전년동기 10% 미만 + YoY 200% 초과)
  - 영업손실 심화 (YoY -50% 이하)
  - 일회성 이익 (기타영업수익 > 영업이익 50%)

grade_change:
  'new'      : 이번 분기 처음 등급 진입
  'up'       : 등급 상승 (예: B→A, A→S)
  'down'     : 등급 하락
  'maintain' : 동일 등급 유지
  'exit'     : 이전 분기에 등급이 있었으나 이번에 없어진 경우 (별도 처리 불필요)

score (정렬용):
  S=100+(yoy+op_yoy+margin), A=80~85+(yoy), B=50+(yoy), 관찰=30+(qoq)
"""

from typing import Optional
from format_utils import get_prev_quarter  # 공통 유틸로 이관
from logger_config import get_logger
from collect_utils import batch_upsert, safe_execute

log = get_logger(__name__)

MIN_REVENUE = 5_000_000_000  # 50억
GRADE_RANK  = {'S': 4, 'A': 3, 'B': 2, '관찰': 1}


def grade_row(r: dict, prev_cache: dict, prev_year_cache: dict,
              prev2_cache: dict = None) -> Optional[dict]:
    """
    단일 종목 행(r)의 등급을 계산해 반환. 해당 없으면 None.
    Returns: {'grade': str, 'score': int} 또는 None
    """
    rev    = r.get('revenue') or 0
    op     = r.get('operating_profit')
    yoy    = r.get('revenue_yoy')
    qoq    = r.get('revenue_qoq')
    op_yoy = r.get('op_profit_yoy')
    margin = r.get('operating_margin')
    ooi    = r.get('other_operating_income') or 0

    prev_r   = prev_cache.get(r['stock_code'], {})
    prev2_r  = (prev2_cache or {}).get(r['stock_code'], {})
    prev_yr  = prev_year_cache.get(r['stock_code'], {})
    prev_margin  = prev_yr.get('operating_margin')
    prev_rev_y   = prev_yr.get('revenue') or 0

    # ── 제외 조건 ──
    if abs(rev) < MIN_REVENUE:
        return None
    if prev_rev_y and abs(prev_rev_y) < abs(rev) * 0.1 and yoy and yoy > 200:
        return None
    if op is not None and op < 0 and op_yoy is not None and op_yoy < -50:
        return None
    if ooi and op and op > 0 and ooi > op * 0.5:
        return None

    grade = None
    score = 0

    # ── S급 ──
    if yoy is not None and yoy >= 30 and op_yoy is not None and op_yoy >= 20:
        margin_ok  = (margin is not None and prev_margin is not None
                      and (margin - prev_margin) >= 2)
        prev_yoy   = prev_r.get('revenue_yoy')
        continuous = (prev_yoy is not None and prev_yoy >= 20) if prev_r else False
        if margin_ok and continuous:
            grade = 'S'
            score = int(100 + (yoy or 0) + (op_yoy or 0) + (margin or 0))
        elif margin_ok or continuous:
            grade = 'A'
            score = int(85 + (yoy or 0) + (op_yoy or 0))

    # ── A급 ──
    if not grade and yoy is not None and yoy >= 30:
        black_turn = prev_rev_y < 0 and rev > 0
        op_good    = op_yoy is not None and op_yoy >= 0
        if black_turn or op_good:
            grade = 'A'
            score = int(80 + (yoy or 0))

    # ── B급 ──
    if not grade and yoy is not None and yoy >= 20 and op is not None and op >= 0:
        grade = 'B'
        score = int(50 + (yoy or 0))

    # ── 관찰 ──
    if not grade and qoq is not None and qoq >= 20:
        prev_op   = prev_r.get('operating_profit') or 0
        prev2_op  = prev2_r.get('operating_profit') or 0
        prev_rev  = prev_r.get('revenue') or 0
        prev2_rev = prev2_r.get('revenue') or 0

        black_q  = prev_op < 0 and (op or 0) > 0
        is_turn  = prev_rev < prev2_rev and rev > prev_rev
        loss_red = ((op or 0) < 0 and prev_op < 0
                    and (op or 0) > prev_op
                    and prev2_r and prev_op > prev2_op)

        if black_q or is_turn or loss_red:
            grade = '관찰'
            score = int(30 + (qoq or 0))

    if not grade:
        return None
    return {'grade': grade, 'score': score}


def save_grade_history(sb, year: str, quarter: str) -> dict:
    """
    해당 분기의 등급을 계산해 earnings_grade_history에 upsert.
    grade_change 컬럼도 함께 저장 (new/up/down/maintain).

    Returns:
        {
          'saved': int,           # 저장 건수
          'new': [row, ...],      # 신규진입
          'up':  [row, ...],      # 등급향상
          'down':[row, ...],      # 등급하락
        }
    """
    log.info(f"📊 [등급이력] {year} {quarter} 등급 계산 시작")

    # ── 해당 분기 재무 데이터 전체 조회 ──
    from db_utils import fetch_all_pages
    all_rows = fetch_all_pages(
        sb.table('financials')
          .select('stock_code,corp_name,bsns_year,quarter,'
                  'revenue,operating_profit,operating_margin,'
                  'revenue_yoy,op_profit_yoy,other_operating_income,'
                  'revenue_qoq,op_profit_qoq')
          .eq('bsns_year', year).eq('quarter', quarter).eq('fs_div', 'CFS')
    )

    if not all_rows:
        log.info(f"📊 [등급이력] {year} {quarter} 재무 데이터 없음 — 스킵")
        return {'saved': 0, 'new': [], 'up': [], 'down': []}

    # ── 직전 분기 캐시 ──
    prev_y, prev_q = get_prev_quarter(year, quarter)
    prev_cache = {}
    if prev_q and prev_y:
        res = safe_execute(sb.table('financials')
               .select('stock_code,revenue,operating_profit,operating_margin,revenue_yoy')
               .eq('bsns_year', prev_y).eq('quarter', prev_q).eq('fs_div', 'CFS'), label='grade-prev')
        for r in (res.data or []):
            prev_cache[r['stock_code']] = r

    # ── 전전 분기 캐시 ──
    prev2_cache = {}
    prev2_y, prev2_q = get_prev_quarter(prev_y or year, prev_q or quarter) if prev_q else (None, None)
    if prev2_q and prev2_y:
        res = safe_execute(sb.table('financials')
               .select('stock_code,revenue,operating_profit')
               .eq('bsns_year', prev2_y).eq('quarter', prev2_q).eq('fs_div', 'CFS'), label='grade-prev2')
        for r in (res.data or []):
            prev2_cache[r['stock_code']] = r

    # ── 전년동기 캐시 ──
    prev_year_cache = {}
    res = safe_execute(sb.table('financials')
           .select('stock_code,revenue,operating_profit,operating_margin')
           .eq('bsns_year', str(int(year) - 1)).eq('quarter', quarter).eq('fs_div', 'CFS'), label='grade-prevyear')
    for r in (res.data or []):
        prev_year_cache[r['stock_code']] = r

    # ── 이전 분기 등급 이력 (grade_change 계산용) ──
    prev_grade_cache = {}  # stock_code → 이전 분기 grade
    if prev_y and prev_q:
        res = safe_execute(sb.table('earnings_grade_history')
               .select('stock_code,grade')
               .eq('bsns_year', prev_y).eq('quarter', prev_q), label='grade-prevgrade')
        for r in (res.data or []):
            prev_grade_cache[r['stock_code']] = r['grade']

    # ── 등급 계산 + grade_change 결정 ──
    records = []
    result_map = {'new': [], 'up': [], 'down': []}

    for r in all_rows:
        result = grade_row(r, prev_cache, prev_year_cache, prev2_cache)
        if not result:
            continue

        cur_grade  = result['grade']
        prev_grade = prev_grade_cache.get(r['stock_code'])

        if prev_grade is None:
            grade_change = 'new'
        elif GRADE_RANK.get(cur_grade, 0) > GRADE_RANK.get(prev_grade, 0):
            grade_change = 'up'
        elif GRADE_RANK.get(cur_grade, 0) < GRADE_RANK.get(prev_grade, 0):
            grade_change = 'down'
        else:
            grade_change = 'maintain'

        row = {
            'stock_code':       r['stock_code'],
            'corp_name':        r['corp_name'],
            'bsns_year':        year,
            'quarter':          quarter,
            'grade':            cur_grade,
            'grade_change':     grade_change,
            'score':            result['score'],
            'rev_yoy':          r.get('revenue_yoy'),
            'op_yoy':           r.get('op_profit_yoy'),
            'revenue':          r.get('revenue'),
            'operating_profit': r.get('operating_profit'),
            'operating_margin': r.get('operating_margin'),
        }
        records.append(row)

        if grade_change in ('new', 'up', 'down'):
            result_map[grade_change].append(row)

    # ── upsert (100개 배치) — 실패 시 중단(기존 try/except 없음 동작 유지) ──
    batch_upsert(sb, 'earnings_grade_history', records,
                 'stock_code,bsns_year,quarter', chunk=100, raise_on_error=True)

    log.info(
        f"📊 [등급이력] {year} {quarter} — {len(records)}개 저장 "
        f"(신규 {len(result_map['new'])}개, 향상 {len(result_map['up'])}개, "
        f"하락 {len(result_map['down'])}개)"
    )
    return {'saved': len(records), **result_map}


# ══════════════════════════════════════════════════════════════════════════════
#  재무 추세 신호 자동 감지
# ══════════════════════════════════════════════════════════════════════════════

def detect_trend_flags(quarters: list) -> dict:
    """
    단일 종목의 시계열 분기 데이터에서 추세 경고 신호 탐지.
    quarters: bsns_year/quarter 오름차순 정렬된 재무 행 리스트
    Returns: {'rev_slowdown': bool, 'op_leverage_fail': bool, 'debt_surge': bool}
    """
    flags = {
        'rev_slowdown':      False,
        'op_leverage_fail':  False,
        'debt_surge':        False,
    }
    if len(quarters) < 2:
        return flags

    # ── 매출 성장 둔화: revenue_qoq가 3분기 연속 하락 ──
    qoq_vals = [r.get('revenue_qoq') for r in quarters if r.get('revenue_qoq') is not None]
    if len(qoq_vals) >= 3:
        tail = qoq_vals[-3:]
        if tail[0] > tail[1] > tail[2]:
            flags['rev_slowdown'] = True

    # ── 영업레버리지 역전: 매출 YoY↑ but 영업익 YoY↓ ──
    last = quarters[-1]
    rev_yoy = last.get('revenue_yoy')
    op_yoy  = last.get('op_profit_yoy')
    if rev_yoy is not None and op_yoy is not None and rev_yoy > 5 and op_yoy < -10:
        flags['op_leverage_fail'] = True

    # ── 부채비율 급증: (total_liabilities / total_equity) QoQ +30%p 이상 ──
    if len(quarters) >= 2:
        prev_q = quarters[-2]
        cur_q  = quarters[-1]
        p_eq   = prev_q.get('total_equity') or 0
        c_eq   = cur_q.get('total_equity')  or 0
        p_debt = prev_q.get('total_liabilities')   or 0
        c_debt = cur_q.get('total_liabilities')    or 0
        if p_eq > 0 and c_eq > 0:
            prev_ratio = p_debt / p_eq * 100
            cur_ratio  = c_debt / c_eq * 100
            if (cur_ratio - prev_ratio) >= 30:
                flags['debt_surge'] = True

    return flags


def save_trend_flags(sb, year: str, quarter: str) -> int:
    """
    해당 분기 financials 레코드에 trend_flags JSONB 저장.
    최근 5분기 데이터 기반 신호 탐지 후 financials 테이블 update.

    사전 조건: ALTER TABLE financials ADD COLUMN IF NOT EXISTS trend_flags JSONB;

    Returns: 플래그가 1개 이상인 종목 수
    """
    from db_utils import fetch_all_pages

    log.info(f"📈 [추세신호] {year} {quarter} 계산 시작")

    target_rows = fetch_all_pages(
        sb.table('financials')
          .select('stock_code')
          .eq('bsns_year', year).eq('quarter', quarter).eq('fs_div', 'CFS')
    )
    if not target_rows:
        log.info(f"📈 [추세신호] {year} {quarter} 데이터 없음 — 스킵")
        return 0

    codes = [r['stock_code'] for r in target_rows]

    # 현재 분기 포함 최근 5분기 수집
    quarters_needed = [(year, quarter)]
    y, q = year, quarter
    for _ in range(4):
        y, q = get_prev_quarter(y, q)
        if y and q:
            quarters_needed.append((y, q))
    years_set    = list({yy for yy, _ in quarters_needed})
    quarters_set = list({qq for _, qq in quarters_needed})

    all_hist = []
    for i in range(0, len(codes), 200):
        batch = codes[i:i + 200]
        res = safe_execute(sb.table('financials')
               .select('stock_code,bsns_year,quarter,revenue,operating_profit,'
                       'revenue_yoy,op_profit_yoy,revenue_qoq,total_liabilities,total_equity')
               .in_('bsns_year', years_set)
               .in_('quarter',   quarters_set)
               .eq('fs_div', 'CFS')
               .in_('stock_code', batch), label='trend-hist')
        all_hist.extend(res.data or [])

    by_code = {}
    for r in all_hist:
        by_code.setdefault(r['stock_code'], []).append(r)

    for code in by_code:
        by_code[code].sort(key=lambda x: (x['bsns_year'], x['quarter']))

    flagged = 0
    failed = 0
    consec_fail = 0
    for code, hist in by_code.items():
        flags = detect_trend_flags(hist)
        # 신호 없는 경우도 빈 dict로 저장해 명시적으로 "검사 완료" 표시
        try:
            safe_execute(sb.table('financials')
               .update({'trend_flags': flags})
               .eq('stock_code', code)
               .eq('bsns_year',  year)
               .eq('quarter',    quarter)
               .eq('fs_div',     'CFS'), retries=3, base_sleep=1.0, label='trend-update')
            consec_fail = 0
        except Exception as e:
            # 종목 하나 실패가 루프 전체를 죽이지 않게 격리 — 다음 실행이 자동 보정.
            # 단 연속 실패가 쌓이면 진짜 연결장애로 보고 조기 중단(무의미한 그라인딩 방지).
            failed += 1
            consec_fail += 1
            if consec_fail >= 20:
                raise RuntimeError(
                    f"추세신호 연속 {consec_fail}개 업데이트 실패 — 연결 장애로 중단 "
                    f"(총 {failed}개 실패): {e}")
            continue
        if any(flags.values()):
            flagged += 1

    if failed:
        log.warning(f"📈 [추세신호] {failed}/{len(by_code)}개 업데이트 실패 "
                    f"(일시 연결 불안정) — 다음 실행 시 자동 보정")
    log.info(f"📈 [추세신호] {year} {quarter} — {flagged}개 경고 신호 (전체 {len(by_code)}개 종목)")
    return flagged
