"""
collect_backlog.py — 정기보고서 '매출 및 수주상황'의 수주잔고 합계 수집 (2026-10-18)

동종 비교(종목리포트 '동종 비교' 카드 · 기업 비교 '수주잔고' 지표)용. 대상 = 반도체 장비
(companies.sub_industry 전공정장비·후공정장비). DART 분기·반기·사업보고서 본문(document.xml)의
'매출 및 수주상황' 절에서 수주잔고 표를 찾아 기말 잔고 합계를 백만원으로 저장한다.
저장 = dart_segment_revenue(segment_type='backlog_total', category='합계') — MD 업로드분
(segment_type='backlog', 품목별)과 섞이지 않게 따로 둔다(사업분석 탭은 'backlog'만 읽음).

표 모양이 회사마다 다르다(10-18 반기보고서 실측: 테스·넥스틴·유니셈 …).
  - 잔고 열: 머리글에 '잔고' — 전기말·기초·전년 잔고는 빼고, 수량·금액이 함께 있으면 금액. 여럿이면 마지막(당기말).
  - 행: 합계·총계 행이 있으면 그 값, 없으면 품목 행 합(소계 제외).
  - 단위: 표 바로 앞 '(단위 : …)' 또는 표 안. 원·천원·백만원·억원만 — 외화·단위 없음은 저장 안 함.
  - 표가 없거나 해당 없음이면 저장 안 함(유진테크·케이씨텍·HPSP 반기보고서엔 수주잔고 표가 없다).

실행:
  python3 collect_backlog.py --probe                 # 대상 종목 최신 보고서 1건씩 읽기만(저장 X)
  python3 collect_backlog.py --backfill 2023         # 2023년~ 정기보고서 전부 (있는 분기는 건너뜀, --force면 다시)
  python3 collect_backlog.py --backfill 2023 --codes 240810,095610 --dry
일일 잡(jobs_collect.job_collect_backlog): run_recent(7) — 최근 7일 정기보고서 중 대상 종목.
"""
import argparse
import html
import logging
import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, '/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SUBS = ('전공정장비', '후공정장비')
SEG = 'backlog_total'
VERBOSE = False     # 명령줄 실행 때 읽은 결과를 한 줄씩 바로 찍기
_REPORT = re.compile(r'(분기|반기|사업)보고서\s*\((\d{4})\.(\d{2})\)')
_QMAP = {'03': 'Q1', '06': 'Q2', '09': 'Q3', '12': 'Q4'}
_TITLE = re.compile(r'<TITLE[^>]*>(.*?)</TITLE>', re.S | re.I)
_TOP = re.compile(r'^\s*(\d+|[IVX]+|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)\s*\.')
_NUM = re.compile(r'^\(?-?[\d,]+(\.\d+)?\)?$')
_TOTAL = re.compile(r'^(합계|총계|계|총합계|합|total)$', re.I)
_EXCL = ('전기', '기초', '전년', '이월', '전분기', '직전', '전반기')
_UNIT = re.compile(r'단위\s*[:：]?\s*([^)\]]{1,25})')


def _txt(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    return re.sub(r'\s+', ' ', html.unescape(s)).strip()


def _num(s):
    s = (s or '').replace(' ', '')
    if not _NUM.match(s) or not re.search(r'\d', s):
        return None
    neg = s.startswith('(') or s.startswith('-')
    v = float(s.strip('()-').replace(',', ''))
    return -v if neg else v


def _unit_factor(u):
    """단위 문구 → 백만원 배수. 외화·모름은 None."""
    u = (u or '').replace(' ', '')
    if re.search(r'USD|US\$|달러|\$|천불|EUR|유로|JPY|엔화|CNY|위안', u, re.I):
        return None
    if '백만원' in u:
        return 1.0
    if '억원' in u:
        return 100.0
    if '천원' in u:
        return 1e-3
    if re.search(r'(^|[:：,(])원', u) or u == '원':
        return 1e-6
    return None


def _section(xml):
    """'매출 및 수주상황'(또는 '수주상황') 제목부터 다음 큰 제목(숫자·로마자 번호) 전까지."""
    ts = [(m.start(), _txt(m.group(1))) for m in _TITLE.finditer(xml)]
    for i, (pos, t) in enumerate(ts):
        if '수주' in t and ('상황' in t or '현황' in t):
            end = next((p for p, tt in ts[i + 1:] if _TOP.match(tt)), len(xml))
            return xml[pos:end]
    return ''


def _grid(table_xml):
    soup = BeautifulSoup(table_xml, 'html.parser')
    grid, carry = [], {}
    for r, tr in enumerate(soup.find_all('tr')):
        row, c = [], 0
        for cell in tr.find_all(['th', 'td', 'te', 'tu'], recursive=False):
            while (r, c) in carry:
                row.append(carry.pop((r, c)))
                c += 1
            t = re.sub(r'\s+', ' ', cell.get_text(' ')).strip()
            try:
                cs, rs = int(cell.get('colspan') or 1), int(cell.get('rowspan') or 1)
            except ValueError:
                cs, rs = 1, 1
            for k in range(cs):
                row.append(t)
                for rr in range(1, rs):
                    carry[(r + rr, c + k)] = t
            c += cs
        while (r, c) in carry:
            row.append(carry.pop((r, c)))
            c += 1
        grid.append(row)
    return [g for g in grid if any(x for x in g)]


def _layout(grid, end_col=False):
    """→ (머리글 행 수, 열 이름들, 잔고 금액 열 후보) 또는 None(머리글과 값을 못 가름)."""
    hdr_n = 0
    for row in grid:
        if any(_num(x) is not None for x in row[1:]):
            break
        hdr_n += 1
    if hdr_n == 0 or hdr_n >= len(grid):
        return None
    ncol = max(len(r) for r in grid)
    labels = []
    for j in range(ncol):
        parts = []
        for h in grid[:hdr_n]:
            if j < len(h) and h[j] and h[j] not in parts:
                parts.append(h[j])
        labels.append(' '.join(parts))
    cands = [j for j, lab in enumerate(labels)
             if ('잔고' in lab or (end_col and ('기말' in lab or '잔액' in lab))) and not any(x in lab for x in _EXCL)]
    return hdr_n, labels, [j for j in cands if '수량' not in labels[j]]


def _read_table(grid, end_col=False):
    """→ (값, 설명) 또는 (None, 이유). 값은 표 단위 그대로.
    end_col: 표 앞 문구가 '계약잔액·잔고'인 표 — 머리글에 '잔고'가 없고 '기초 | 신규계약 | 수익인식 | 기말'로
    적는 회사(유진테크)는 '기말'(또는 '잔액') 열이 잔고다."""
    lay = _layout(grid, end_col)
    if not lay:
        return None, '머리글/값 구분 안 됨'
    hdr_n, labels, amt = lay
    if not amt:
        # 행으로 적은 표 — '수주잔고 | 금액'(한빛레이저), '기말공사계약잔액 | 당반기 | 전기'(케이씨텍):
        # 첫 칸에 '잔고'(계약잔액 표면 '기말')가 든 행 — 기간 열이 있으면 '당…' 열, 없으면 마지막 숫자
        def is_bl(c0):
            return ('잔고' in c0 or (end_col and '기말' in c0)) and not any(x in c0 for x in _EXCL)
        rows = [r for r in grid[hdr_n:] if r and is_bl(r[0])]
        if rows:
            r = rows[-1]
            cur = [j for j, lab in enumerate(labels) if j > 0 and lab.replace(' ', '').startswith('당') and j < len(r)]
            if cur and _num(r[cur[0]]) is not None:
                return _num(r[cur[0]]), f'행 "{r[0]}" · 열 "{labels[cur[0]]}"'
            nums = [v for v in (_num(x) for x in r[1:]) if v is not None]
            if nums:
                return nums[-1], f'행 "{r[0]}"'
        return None, f'잔고 금액 열 없음 {labels}'
    col = amt[-1]
    data = grid[hdr_n:]
    tot = [r for r in data if any(_TOTAL.match((x or '').replace(' ', '')) for x in r[:3])]
    if tot:
        v = _num(tot[-1][col]) if col < len(tot[-1]) else None
        if v is not None:
            return v, f'합계 행 · 열 "{labels[col]}"'
    vals = [_num(r[col]) for r in data if col < len(r) and not any('소계' in (x or '') for x in r[:3])]
    vals = [v for v in vals if v is not None]
    if vals:
        return sum(vals), f'품목 {len(vals)}행 합 · 열 "{labels[col]}"'
    # 잔고 금액 칸이 '-'뿐 — 수주총액 − 기납품액(잔고의 정의). 한미반도체 2026 반기: 총액 442억·기납품 '-' → 442억,
    # 프로이천: 총액 = 기납품 → 0
    def _amt_col(key):
        cs = [j for j, lab in enumerate(labels) if key in lab.replace(' ', '') and '수량' not in lab]
        return cs[-1] if cs else None
    jt, jd = _amt_col('수주총액'), _amt_col('기납품')
    row = tot[-1] if tot else None
    if row and jt is not None and jd is not None and max(jt, jd) < len(row):
        t, d = _num(row[jt]), _num(row[jd])
        if t is not None:
            return t - (d or 0), '합계 행 수주총액 − 기납품액(잔고 칸 비어 있음)'
    return None, '값 없음'


# 단위 문구 조각 → (통화, 배수). 'K$, 백만원'(네오셈) = 달러는 천 달러, 원화는 백만원
_UNIT_TOKENS = [
    (r'^(K\$|천USD|USD천|천달러|천불)$', 'USD', 1e3),
    (r'^(백만USD|USD백만|백만달러|M\$)$', 'USD', 1e6),
    (r'^(USD|US\$|\$|달러|미화|불)$',    'USD', 1.0),
    (r'^백만원$', 'KRW', 1e6), (r'^억원$', 'KRW', 1e8), (r'^천원$', 'KRW', 1e3), (r'^원$', 'KRW', 1.0),
]


def _unit_tokens(u):
    out = {}
    for tok in re.split(r'[,/·]', (u or '').replace(' ', '')):
        for pat, ccy, mult in _UNIT_TOKENS:
            if re.match(pat, tok, re.I):
                out.setdefault(ccy, mult)
                break
    return out


def _read_fx(grid, unit_txt, period, end_col=False):
    """외화로 적은 잔고 표 → (백만원, 설명) 또는 (None, 이유). 잔고는 기말 잔액이라 기말 환율
    (fx_history.close_rate — 재무 외화 환산과 같은 출처, 재무상태표 규칙).
      - 코세스: 표 전체가 USD → 합계 × 환율
      - 네오셈: '통화' 열(USD·KRW) + 단위 'K$, 백만원' → 통화별 품목 합을 각각 원화로 바꿔 더함"""
    toks = _unit_tokens(unit_txt)
    fx = [c for c in toks if c != 'KRW']
    if len(fx) != 1 or not period:
        return None, f'외화 단위 모름 ({unit_txt})'
    ccy = fx[0]
    try:
        import fx_history
        rate = fx_history.close_rate(ccy, fx_history.quarter_bounds(*period)[2])
    except Exception as e:
        return None, f'{ccy} 기말 환율 없음 ({str(e)[:40]})'
    lay = _layout(grid, end_col)
    if not lay or not lay[2]:
        return None, '외화 표 잔고 열 없음'
    hdr_n, labels, amt = lay
    col = amt[-1]
    j_cur = next((j for j, lab in enumerate(labels) if '통화' in lab), None)
    if j_cur is None:                                    # 표 전체가 한 외화
        if 'KRW' in toks:
            return None, f'외화·원화 섞인 단위인데 통화 열 없음 ({unit_txt})'
        v, how = _read_table(grid, end_col)
        if v is None:
            return None, how
        return v * toks[ccy] * rate / 1e6, f'{how} · 단위 {unit_txt} · 기말 환율 {rate:,.1f}원'
    sums = {}
    for r in grid[hdr_n:]:
        lab0 = ' '.join(r[:3])
        if '합계' in lab0 or '소계' in lab0 or any(_TOTAL.match(re.sub(r'\(.*?\)', '', x or '').replace(' ', '')) for x in r[:3]):
            continue
        cur = (r[j_cur] if j_cur < len(r) else '').upper().strip()
        v = _num(r[col]) if col < len(r) else None
        if cur and v is not None:
            sums[cur] = sums.get(cur, 0) + v
    if not sums:
        return None, '외화 표 값 없음'
    total, parts = 0.0, []
    for cur, s in sums.items():
        if cur in ('KRW', '원'):
            if 'KRW' not in toks:
                return None, f'원화 행 단위 모름 ({unit_txt})'
            total += s * toks['KRW'] / 1e6
        elif cur == ccy:
            total += s * toks[ccy] * rate / 1e6
        else:
            return None, f'통화 {cur} 환산 안 함'
        parts.append(f'{cur} {s:,.0f}')
    return total, f'통화별 품목 합({", ".join(parts)}) · 단위 {unit_txt} · 기말 환율 {rate:,.1f}원'


def _guess_factor(v, near):
    """'(단위 : )'처럼 단위를 비워 둔 보고서(테스 2025 반기·유니테스트 등) — 같은 회사 저장값(백만원) 크기에
    가장 가까운 단위. 10배 넘게 어긋나면 모른다(None)."""
    import math
    if not near or not v:
        return None
    ref = math.log10(near)
    f = min((1.0, 1e-3, 1e-6, 100.0), key=lambda f: abs(math.log10(abs(v * f)) - ref))
    return f if abs(math.log10(abs(v * f)) - ref) < 1 else None


def parse_backlog(xml, near=None, period=None):
    """보고서 본문 → (백만원, 설명) 또는 (None, 이유).
    near: 단위가 빈칸일 때 부를 함수 → 같은 회사 저장 잔고의 중앙값(백만원) 또는 None.
    period: (연도, 'Qn') — 외화 잔고를 기말 환율로 바꿀 때."""
    sec = _section(xml)
    if not sec:
        return None, '수주상황 절 없음'
    found = []
    for m in re.finditer(r'<TABLE.*?</TABLE>', sec, re.S | re.I):
        tx = m.group(0)
        head, before = _txt(tx)[:300], _txt(sec[max(0, m.start() - 600):m.start()])
        # 표 앞 문구가 '…계약잔액·잔고' + 머리글에 '기말' — 유진테크 '(1) 당반기말 장비수주 계약잔액 | 기초 신규계약 수익인식 기말'
        end_col = '잔고' not in _txt(tx) and '기말' in head and ('잔액' in before[-200:] or '잔고' in before[-200:])
        if '잔고' not in _txt(tx) and not end_col:
            continue
        # 전기말 표 건너뜀 — 바로 앞 문구에서 '전기·전년'이 '당기·당반기…'보다 뒤에 나오면 지난 기간 표
        # (유진테크는 '(2) 전기말 장비수주 계약잔액'을 같은 모양으로 한 번 더 싣는다 — 합치면 두 배)
        last = lambda ws: max((before.rfind(w) for w in ws), default=-1)
        if last(('전기말', '전기 말', '전년말', '전반기말', '전분기말')) > last(('당기', '당반기', '당분기', '당기말')):
            continue
        # 단위: 표 안 머리 → 없으면 표 바로 앞에서 가장 가까운 것(단위 줄이 따로 작은 표로 붙는 일이 많다)
        units = _UNIT.findall(head) or _UNIT.findall(_txt(sec[max(0, m.start() - 1500):m.start()]))[-1:]
        if units and set(_unit_tokens(units[-1])) - {'KRW'}:      # 외화 단위(코세스 USD, 네오셈 K$·백만원)
            v, how = _read_fx(_grid(tx), units[-1].strip(), period, end_col)
            found.append((round(v) if v is not None else None, how))
            continue
        v, how = _read_table(_grid(tx), end_col)
        if v is None:
            found.append((None, how))
            continue
        f = _unit_factor(units[-1]) if units else None
        unit_note = f'단위 {units[-1].strip()}' if units else ''
        if f is None and units and not units[-1].strip() and near:
            f = _guess_factor(v, near())
            unit_note = '단위 빈칸 → 저장된 잔고 크기로 추정'
        if f is None:
            found.append((None, f'단위 못 읽음/외화 ({units[-1] if units else "없음"})'))
            continue
        found.append((round(v * f), f'{how} · {unit_note}'))
    ok = [x for x in found if x[0] is not None]
    if not ok:
        return None, (found[0][1] if found else '잔고 표 없음')
    if len(ok) > 1:
        # 잔고 표가 여럿이면 회사·부문별로 나눈 것 → 합(연결). 에스티아이(본사 + 자회사)·원익홀딩스(본사 + 원익IPS).
        # 같은 값이 되풀이되면(요약·상세) 하나로 센다.
        vals = list(dict.fromkeys(x[0] for x in ok))
        return sum(vals), ok[0][1] + f' (잔고 표 {len(ok)}개 합: {vals})'
    return ok[0]


# ── DART ──────────────────────────────────────────────────────────────────────
_dart = None


def _get_dart():
    global _dart
    if _dart is None:
        import OpenDartReader
        _dart = OpenDartReader(os.environ['DART_API_KEY'])
    return _dart


def _period(report_nm):
    m = _REPORT.search(report_nm or '')
    if not m or m.group(3) not in _QMAP:
        return None
    return m.group(2), _QMAP[m.group(3)]


def _targets(sb, codes=None):
    q = sb.table('companies').select('code,name,corp_code,sub_industry').eq('industry', '반도체') \
        .in_('sub_industry', list(SUBS)).eq('active', True)
    rows = [r for r in (q.execute().data or []) if r.get('corp_code') and r.get('code')]
    if codes:
        rows = [r for r in rows if r['code'] in codes]
    return rows


def _existing(sb, codes):
    have = set()
    for i in range(0, len(codes), 100):
        for r in sb.table('dart_segment_revenue').select('stock_code,bsns_year,quarter') \
                .eq('segment_type', SEG).in_('stock_code', codes[i:i + 100]).execute().data or []:
            have.add((r['stock_code'], str(r['bsns_year']), r['quarter']))
    return have


def _near(sb, code):
    """같은 회사 저장 잔고(0 제외)의 중앙값 — 단위 빈칸 보고서의 단위 추정용."""
    vs = sorted(r['revenue'] for r in sb.table('dart_segment_revenue').select('revenue')
                .eq('segment_type', SEG).eq('stock_code', code).execute().data or [] if r['revenue'])
    return vs[len(vs) // 2] if vs else None


def _save(sb, code, y, q, mil):
    sb.table('dart_segment_revenue').upsert({
        'stock_code': code, 'bsns_year': int(y), 'quarter': q, 'segment_type': SEG,
        'category': '합계', 'subcategory': '', 'revenue': mil, 'revenue_ratio': None,
    }, on_conflict='stock_code,bsns_year,quarter,segment_type,category,subcategory').execute()


def _collect(sb, t, reports, have, force, save, out):
    for rno, nm in reports:
        p = _period(nm)
        if not p:
            continue
        y, q = p
        if not force and (t['code'], y, q) in have and '정정' not in nm:
            continue
        try:
            xml = _get_dart().document(rno)
        except Exception as e:
            out.append((t['name'], y, q, None, f'본문 실패 {str(e)[:60]}'))
            continue
        v, how = parse_backlog(xml or '', near=lambda: _near(sb, t['code']), period=(y, q))
        out.append((t['name'], y, q, v, how))
        if VERBOSE:
            print(t['name'], y, q, v, how, sep=' | ', flush=True)
        if v is not None and save:
            _save(sb, t['code'], y, q, v)
            have.add((t['code'], y, q))
        time.sleep(0.2)


def _reports(corp_code, start, end):
    df = _get_dart().list(corp_code, start=start, end=end, kind='A', final=True)
    if df is None or not len(df):
        return []
    df = df.sort_values('rcept_no')
    return list(zip(df['rcept_no'], df['report_nm']))


def run_backfill(from_year=2023, codes=None, force=False, save=True, workers=4):
    """보고서 본문 한 건이 4MB 안팎·~15초라(사업보고서 zip 통째) 종목 단위로 나눠 동시에 받는다."""
    from concurrent.futures import ThreadPoolExecutor
    from collect_financials import _get_sb
    sb = _get_sb()
    ts = _targets(sb, codes)
    have = _existing(sb, [t['code'] for t in ts])
    out = []

    def one(t):
        try:
            reps = _reports(t['corp_code'], f'{from_year}-01-01', date.today().isoformat())
        except Exception as e:
            out.append((t['name'], '', '', None, f'목록 실패 {str(e)[:60]}'))
            return
        _collect(sb, t, reps, have, force, save, out)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, ts))
    return out


def run_recent(days=7):
    """최근 days일 정기보고서 중 대상 종목 — 없는 분기 또는 정정 보고서만 읽는다(일일 잡)."""
    from collect_financials import _get_sb
    sb = _get_sb()
    ts = {t['corp_code']: t for t in _targets(sb)}
    df = _get_dart().list(start=(date.today() - timedelta(days=days)).isoformat(),
                          end=date.today().isoformat(), kind='A', final=True)
    if df is None or not len(df):
        return []
    df = df[df['corp_code'].isin(ts.keys())].sort_values('rcept_no')
    have = _existing(sb, [t['code'] for t in ts.values()])
    out = []
    for cc, grp in df.groupby('corp_code'):
        _collect(sb, ts[cc], list(zip(grp['rcept_no'], grp['report_nm'])), have, False, True, out)
    return out


def probe(codes=None):
    from collect_financials import _get_sb
    sb = _get_sb()
    out = []
    for t in _targets(sb, codes):
        try:
            reps = _reports(t['corp_code'], '2026-01-01', date.today().isoformat())
        except Exception as e:
            out.append((t['name'], t['sub_industry'], '', None, f'목록 실패 {e}'))
            continue
        if not reps:
            out.append((t['name'], t['sub_industry'], '', None, '보고서 없음'))
            continue
        rno, nm = reps[-1]
        v, how = parse_backlog(_get_dart().document(rno) or '', period=_period(nm))
        out.append((t['name'], t['sub_industry'], nm, v, how))
        time.sleep(0.2)
    return out


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument('--probe', action='store_true')
    ap.add_argument('--backfill', type=int)
    ap.add_argument('--codes')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    codes = a.codes.split(',') if a.codes else None
    if a.probe:
        for r in probe(codes):
            print(*r, sep=' | ')
    elif a.backfill:
        VERBOSE = True
        res = run_backfill(a.backfill, codes, a.force, save=not a.dry)
        print(f"읽음 {len(res)} · 잔고 {sum(1 for r in res if r[3] is not None)}")
