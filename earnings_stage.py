"""
earnings_stage.py
─────────────────
실적 국면(1·2·2.5·3·4·NA) — 전 종목 분기별 → earnings_stage (화면: 프론트 js/earnings-stage.js '실적 국면' 페이지)

[원본] 태린이아빠 「확정실적 분석」(2026-10 자료) '데이터정리' 시트. DataGuide 분기 확정실적으로
  종목 × 분기마다 국면을 매긴다(직전 분기 국면 P에 따라 갈린다). 한 칸 수식:
    IF(OR(매출g="",영업g="",스프레드="",직전스프레드=""),"",
     IF(AND(P<>2,P<>2.5,매출g>5,매출g>영업g,MAX(최근 8분기 매출)=당분기 매출),1,
      IF(OR(AND(P>3,매출g>5,8분기 최고,영업g>매출g,스프레드>직전),AND(P<3,영업g>매출g,스프레드>직전)),2.5,
       IF(OR(AND(P>3,매출g>5,8분기 최고,영업g>매출g,직전>스프레드),AND(P<3,영업g>매출g,직전>스프레드)),2,
        IF(AND(P<>4,매출g>영업g),3,IF(매출g<0,4,"NA"))))))
  - 매출g·영업g = 전년 동분기 대비 % = (당−전년)/|전년|×100 — DataGuide 증가율(YoY). 원본 값으로
    확인(09-12만 칸 중 반올림 차이 외 일치). 상한 없음.
  - 스프레드 = 매출g − 영업g. 원본 스프레드 시트는 빈칸을 0으로 빼서 늘 숫자다(앞 분기 증가율이
    없으면 직전 스프레드 = 0).
  - 엑셀 비교 규칙 그대로: 빈 셀은 숫자 0, 문자("" · "NA")는 어떤 숫자보다 크다 → 직전이 "NA"/""면
    P>3 참·P<3 거짓(4단계 다음처럼 판정). 맨 앞 칸의 직전은 빈 셀(0).
  - 원본 '실적확인' 시트 표시: 0<국면<2.6 → 매수, 국면>2.9 → 매도. (화면이 쓴다)
  - 원본 입력(DataGuide)으로 이 계산을 돌려 '데이터정리' 51,100칸 전부 일치 확인(2026-10-11).

[원본과 다른 점]
  - 입력: DataGuide 대신 우리 financials(DART 정기보고서, 종목별 연결 우선·없으면 별도 — 프론트
    finPreferFs와 같음). 4분기 누적 행(사업보고서 연간값)은 같은 해 1~3분기가 다 있으면 빼서 4분기
    단독으로, 없으면 버린다(fin_rules.is_annual_q4 규칙, 프론트 finNormalizeQuarters와 같음).
  - financials 이력이 2023년 1분기부터라 증가율은 2024년 1분기부터, 국면도 그때부터 계산한다.
    국면은 직전 국면에 따라 갈려서 앞쪽 분기는 원본과 덜 맞는다. 원본 '값복사' 결과와 대조(10-11):
    2024Q2 57% → 2025Q2 78% → 2026Q2 84% 일치. 2026Q2 불일치 413건 = 우리 매출 없음 152(보험·증권 등
    금융사 — DART 매출액 칸 없음) · 전년 없음 105(2025년 이후 상장, DataGuide는 상장 전 값 보유) ·
    입력 차이 52(지주사 등 DataGuide와 DART 값이 다름) · 직전 국면·8분기 차이 71 · 원본 빈칸 24 등.
  - 원본은 '스프레드' 시트가 26행 짧아 마지막 26종목(해외 상장사·최근 상장)의 국면이 비어 있다. 여기선 계산한다.

실행:
  python3 earnings_stage.py              # 전 종목 계산 → earnings_stage 저장 (일일 잡과 같음)
  python3 earnings_stage.py --dry        # 저장 없이 분포만 출력
  python3 earnings_stage.py --check 005930   # 한 종목 분기별 계산 출력
"""
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

TABLE = 'earnings_stage'
SALES_MIN = 5          # 원본 B7 '매출성장률 :' 5(%)
WINDOW = 8             # MAX(매출액 8칸) — 당분기 포함 최근 8분기
TRAIL = 8              # 화면 목록에 보이는 최근 국면 개수


# ── 순수 계산 ─────────────────────────────────────────────────────

def _xl(v):
    """엑셀 비교 순서 키: 빈 셀=숫자 0 < 숫자 < 문자."""
    if v is None:
        return (0, 0.0)
    if isinstance(v, (int, float)):
        return (0, float(v))
    return (1, str(v).upper())


def _gt(a, b): return _xl(a) > _xl(b)
def _lt(a, b): return _xl(a) < _xl(b)
def _ne(a, b): return _xl(a) != _xl(b)


def yoy(cur, prev):
    """DataGuide 증가율(YoY): (당−전년)/|전년|×100. 값이 없거나 전년이 0이면 None."""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100


def stage_of(prev_stage, sg, og, sp, sp_prev, sales_window, sales_cur):
    """한 분기 국면. 반환: 1 · 2 · 2.5 · 3 · 4 · 'NA' · ''(입력 없음). 원본 수식 그대로."""
    if sg is None or og is None or sp is None or sp_prev is None:
        return ''
    nums = [float(x) for x in sales_window if isinstance(x, (int, float))]
    cur = float(sales_cur) if isinstance(sales_cur, (int, float)) else 0.0
    high = (max(nums) if nums else 0.0) == cur
    P = prev_stage
    if _ne(P, 2) and _ne(P, 2.5) and sg > SALES_MIN and sg > og and high:
        return 1
    if ((_gt(P, 3) and sg > SALES_MIN and high and og > sg and sp > sp_prev)
            or (_lt(P, 3) and og > sg and sp > sp_prev)):
        return 2.5
    if ((_gt(P, 3) and sg > SALES_MIN and high and og > sg and sp_prev > sp)
            or (_lt(P, 3) and og > sg and sp_prev > sp)):
        return 2
    if _ne(P, 4) and sg > og:
        return 3
    if sg < 0:
        return 4
    return 'NA'


def quarter_grid(first, last):
    """(연, 분기) 오름차순 격자. first·last 포함."""
    y, q = first
    out = []
    while (y, q) <= last:
        out.append((y, q))
        q += 1
        if q > 4:
            y, q = y + 1, 1
    return out


def prefer_fs(rows):
    """한 종목의 행: CFS가 하나라도 있으면 CFS만, 없으면 OFS만 (프론트 finPreferFs와 같음)."""
    want = 'CFS' if any(r.get('fs_div') == 'CFS' for r in rows) else 'OFS'
    return [r for r in rows if r.get('fs_div') == want], want


def quarter_values(rows):
    """한 종목(같은 fs)의 행 → {(연, 분기): (매출, 영업익)} 분기 단독.
    is_cumulative 4분기 = 사업보고서 연간값 → 같은 해 1~3분기가 다 있으면 빼서 4분기 단독, 없으면 버린다.
    1~3분기 누적 행의 손익은 분기 단독 그대로 맞다(fin_rules 머리말)."""
    by = {}
    for r in rows:
        try:
            k = (int(r['bsns_year']), int(str(r['quarter']).upper().replace('Q', '')))
        except (TypeError, ValueError):
            continue
        by[k] = r
    out = {}
    for (y, q), r in by.items():
        rev, op = r.get('revenue'), r.get('operating_profit')
        if q == 4 and r.get('is_cumulative'):
            q13 = [by.get((y, k)) for k in (1, 2, 3)]
            if not all(q13):
                continue

            def sub(cur, key):
                vals = [x.get(key) for x in q13]
                return None if cur is None or any(v is None for v in vals) else cur - sum(vals)
            rev, op = sub(rev, 'revenue'), sub(op, 'operating_profit')
        out[(y, q)] = (rev, op)
    return out


def compute_stock(qvals, grid, start_idx):
    """qvals: {(연,분기): (매출, 영업익)}. grid: 전체 분기 격자. start_idx: 국면을 매기기 시작하는 칸
    (원본의 L열 — 그 앞 칸 국면은 빈 셀). 반환: 격자 전체 [{y,q,rev,op,sg,og,sp,hi,st}]
    (start_idx 앞 칸은 st=None)."""
    def g(i, k):
        if i < 0:
            return None
        v = qvals.get(grid[i])
        return v[k] if v else None

    def growth(i):
        if i < 4:
            return None, None
        return yoy(g(i, 0), g(i - 4, 0)), yoy(g(i, 1), g(i - 4, 1))

    out = []
    prev_stage = None
    for i in range(len(grid)):
        sg, og = growth(i)
        cur = g(i, 0)
        win = [g(j, 0) for j in range(i - WINDOW + 1, i + 1)]
        nums = [float(x) for x in win if isinstance(x, (int, float))]
        st = None
        if i >= start_idx:
            psg, pog = growth(i - 1)
            sp_ = (sg or 0) - (og or 0)
            spp = (psg or 0) - (pog or 0)
            st = stage_of(prev_stage, sg, og, sp_, spp, win, cur)
            prev_stage = st
        out.append({
            'y': grid[i][0], 'q': grid[i][1], 'rev': cur, 'op': g(i, 1),
            'sg': sg, 'og': og,
            'sp': (sg - og) if (sg is not None and og is not None) else None,
            'hi': bool(nums) and cur is not None and max(nums) == float(cur),
            'st': st,
        })
    return out


def _st(v):
    """국면 값 → 저장 문자열 ('1','2','2.5','3','4','NA') · 빈칸/미계산은 None."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return ('%g' % v)
    return str(v)


def _r(v, n=2):
    return None if v is None else round(float(v), n)


def build_record(code, fs, hist):
    """한 종목 계산 결과 → earnings_stage 행. 최근 분기 = 매출이 있는 마지막 분기."""
    have = [i for i, h in enumerate(hist) if h['rev'] is not None]
    if not have:
        return None
    li = have[-1]
    last = hist[li]
    prev = hist[li - 1] if li > 0 else None
    label = lambda h: f"{h['y']}Q{h['q']}"
    first = next(i for i in have)
    history = [{
        'p': label(h),
        'rev': _r(h['rev'] / 1e8 if h['rev'] is not None else None),   # 억원
        'op': _r(h['op'] / 1e8 if h['op'] is not None else None),
        'sg': _r(h['sg']), 'og': _r(h['og']), 'sp': _r(h['sp']),
        'hi': h['hi'], 'st': _st(h['st']),
    } for h in hist[first:li + 1]]
    trail = [[label(h), _st(h['st'])] for h in hist[max(first, li - TRAIL + 1):li + 1]]
    return {
        'stock_code': code,
        'period': label(last),
        'stage': _st(last['st']),
        'prev_stage': _st(prev['st']) if prev else None,
        'sales_yoy': _r(last['sg']), 'op_yoy': _r(last['og']), 'spread': _r(last['sp']),
        'sales_high': bool(last['hi']),
        'revenue': history[-1]['rev'], 'op': history[-1]['op'],
        'fs_div': fs,
        'trail': trail,
        'history': history,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }


def compute_all(fin_rows, codes=None):
    """financials 행 전체 → (records, grid). 격자는 데이터의 첫 해 1분기 ~ 마지막 분기,
    국면은 증가율이 생기는 5번째 칸(첫 해 다음 해 1분기)부터.
    codes: 남길 종목코드 집합(상장 중인 종목). None이면 6자리 코드면 모두."""
    by = defaultdict(list)
    keys = []
    for r in fin_rows:
        code = r.get('stock_code') or ''
        if len(code) != 6 or (codes is not None and code not in codes):
            continue   # stock_code 빈 행(10-11 기준 229행)·상장폐지 종목 제외
        by[code].append(r)
        try:
            keys.append((int(r['bsns_year']), int(str(r['quarter']).upper().replace('Q', ''))))
        except (TypeError, ValueError):
            pass
    if not keys:
        return [], []
    grid = quarter_grid((min(keys)[0], 1), max(keys))
    start = 4
    recs = []
    for code, rows in by.items():
        rows_fs, fs = prefer_fs(rows)
        hist = compute_stock(quarter_values(rows_fs), grid, start)
        rec = build_record(code, fs, hist)
        if rec:
            recs.append(rec)
    return recs, grid


# ── DB ───────────────────────────────────────────────────────────

def _load_financials(sb):
    from db_utils import fetch_all_pages
    return fetch_all_pages(
        sb.table('financials')
        .select('stock_code,bsns_year,quarter,fs_div,is_cumulative,revenue,operating_profit')
        .order('stock_code').order('bsns_year').order('quarter').order('fs_div'))


def _active_codes(sb):
    """상장 중인 종목코드(6자리) — companies.active. 일부 code는 '005930.KS' 꼴이라 접미사를 뗀다."""
    from db_utils import fetch_all_pages
    rows = fetch_all_pages(sb.table('companies').select('code').eq('active', True))
    return {str(r['code']).split('.')[0] for r in rows if r.get('code')}


def _has_table(sb) -> bool:
    try:
        sb.table(TABLE).select('stock_code').limit(1).execute()
        return True
    except Exception as e:
        if any(m in str(e) for m in ('42P01', 'PGRST205', 'does not exist', 'Could not find')):
            return False
        raise


def run(dry: bool = False) -> dict:
    from db_client import get_supabase_client
    sb = get_supabase_client()
    if not dry and not _has_table(sb):
        print(f"[실적국면] {TABLE} 테이블 없음 (sql/earnings_stage.sql 실행 전) — 스킵")
        return {'stocks': 0, 'skipped': 'no_table'}
    fin = _load_financials(sb)
    recs, grid = compute_all(fin, _active_codes(sb))
    per = Counter(r['period'] for r in recs)
    top = per.most_common(1)[0][0] if per else None
    dist = Counter(r['stage'] for r in recs if r['period'] == top)
    print(f"[실적국면] financials {len(fin)}행 · {len(recs)}종목 · 격자 {grid[0]}~{grid[-1]} · "
          f"최다 기준분기 {top}({per.get(top, 0)}) 분포 {dict(dist)}")
    if dry:
        return {'stocks': len(recs), 'period': top, 'dist': dict(dist)}
    from collect_utils import batch_upsert
    n = batch_upsert(sb, TABLE, recs, 'stock_code', chunk=300, progress_label='실적국면',
                     raise_on_error=True)
    return {'stocks': len(recs), 'saved': n, 'period': top, 'dist': dict(dist)}


def _check(code):
    from db_client import get_supabase_client
    sb = get_supabase_client()
    rows = sb.table('financials').select(
        'stock_code,bsns_year,quarter,fs_div,is_cumulative,revenue,operating_profit').eq(
        'stock_code', code).execute().data
    recs, _ = compute_all(rows)
    if not recs:
        print('데이터 없음')
        return
    r = recs[0]
    print(f"{code} fs={r['fs_div']} 최근 {r['period']} 국면 {r['stage']} (직전 {r['prev_stage']})")
    for h in r['history']:
        print(f"  {h['p']}  매출 {h['rev']}억  영업익 {h['op']}억  매출g {h['sg']}  영업g {h['og']}  "
              f"스프레드 {h['sp']}  8분기최고 {'●' if h['hi'] else ' '}  국면 {h['st']}")


if __name__ == '__main__':
    if '--check' in sys.argv:
        _check(sys.argv[sys.argv.index('--check') + 1])
    else:
        print(run(dry='--dry' in sys.argv))
