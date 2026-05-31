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

import logging
from typing import Optional
from format_utils import get_prev_quarter  # 공통 유틸로 이관
from logger_config import get_logger

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
        res = (sb.table('financials')
               .select('stock_code,revenue,operating_profit,operating_margin,revenue_yoy')
               .eq('bsns_year', prev_y).eq('quarter', prev_q).eq('fs_div', 'CFS')
               .execute())
        for r in (res.data or []):
            prev_cache[r['stock_code']] = r

    # ── 전전 분기 캐시 ──
    prev2_cache = {}
    prev2_y, prev2_q = get_prev_quarter(prev_y or year, prev_q or quarter) if prev_q else (None, None)
    if prev2_q and prev2_y:
        res = (sb.table('financials')
               .select('stock_code,revenue,operating_profit')
               .eq('bsns_year', prev2_y).eq('quarter', prev2_q).eq('fs_div', 'CFS')
               .execute())
        for r in (res.data or []):
            prev2_cache[r['stock_code']] = r

    # ── 전년동기 캐시 ──
    prev_year_cache = {}
    res = (sb.table('financials')
           .select('stock_code,revenue,operating_profit,operating_margin')
           .eq('bsns_year', str(int(year) - 1)).eq('quarter', quarter).eq('fs_div', 'CFS')
           .execute())
    for r in (res.data or []):
        prev_year_cache[r['stock_code']] = r

    # ── 이전 분기 등급 이력 (grade_change 계산용) ──
    prev_grade_cache = {}  # stock_code → 이전 분기 grade
    if prev_y and prev_q:
        res = (sb.table('earnings_grade_history')
               .select('stock_code,grade')
               .eq('bsns_year', prev_y).eq('quarter', prev_q)
               .execute())
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

    # ── upsert (100개 배치) ──
    for i in range(0, len(records), 100):
        sb.table('earnings_grade_history') \
          .upsert(records[i:i + 100], on_conflict='stock_code,bsns_year,quarter') \
          .execute()

    log.info(
        f"📊 [등급이력] {year} {quarter} — {len(records)}개 저장 "
        f"(신규 {len(result_map['new'])}개, 향상 {len(result_map['up'])}개, "
        f"하락 {len(result_map['down'])}개)"
    )
    return {'saved': len(records), **result_map}
