"""
disclosure_digest.py — 메인 채널 저녁 공시 다이제스트 (매일 19:00)

오늘 봇이 발송한 공시(notice_history)를 주요·긴급 등급만 골라 카테고리별로 요약.
실적/계약/청약/상장폐지는 원문 재파싱(get_disclosure_detail)으로 핵심 수치 enrich.

jobs_briefing.job_disclosure_digest 가 generate()를 호출해 DEFAULT_CHAT_ID로 발송.
"""
import re
import logging
import datetime
from collections import OrderedDict

log = logging.getLogger(__name__)

_KST = datetime.timezone(datetime.timedelta(hours=9))
_WD = ['월', '화', '수', '목', '금', '토', '일']


def _amt_num(s: str) -> float:
    """'6조 3,322억' / '598억' → 정렬용 대략치(억 단위)."""
    n = 0.0
    if mo := re.search(r'([\d.]+)\s*조', s):
        n += float(mo.group(1)) * 1e4
    if me := re.search(r'([\d,]+)\s*억', s):
        n += float(me.group(1).replace(',', ''))
    return n


def _earn_metric(detail: str, label: str):
    """잠정실적 본문 '  매출: 3조 4,139억 / QoQ +5.1% / YoY +12.9%' → ('3조 4,139억', '+12.9%').

    금액은 첫 '/' 앞까지 통째로 — 예전 정규식([\\d,조억]+)은 공백에서 멈춰 '3조 4,139억'이
    '3조'로 나갔다(한화오션·HD현대일렉트릭 2026-10-27). YoY는 증감률 또는 흑자/적자 전환 문구."""
    m = re.search(rf'^\s*{label}:\s*([^/\n]+?)\s*(?:/(.*))?$', detail, re.M)
    if not m:
        return '', ''
    y = re.search(r'YoY\s*([+\-]?[\d.]+%|[흑적]자\s*[전지][환속](?:\s*\(손실 [축확][소대]\))?)', m.group(2) or '')
    return m.group(1).strip(), (y.group(1) if y else '')


def _detail_line(detail: str, key: str) -> str:
    for ln in detail.split('\n'):
        head = ln.split(':', 1)[0]
        if key in head:
            return ln.strip()
    return ''


def _field(detail: str, key: str) -> str:
    """'💰 계약금액: 1,439억원 (…)' 같은 항목 줄의 값. 콜론 있는 항목 줄만 본다 — 낱말만 들어간
    줄(지급조건 목록의 '• … / 계약금액 유보기간 종료 후 공개예정 / …')을 집어 원문째 나가던 것 방지."""
    m = re.search(rf'^[^\S\n]*(?:\S+\s)?{key}\s*:\s*(.+)$', detail, re.M)
    return m.group(1).strip() if m else ''


def _contract_amount(detail: str) -> str:
    """계약금액 — 숫자가 없으면(공시유보) '금액 비공개'."""
    a = _field(detail, '계약금액')
    if a and re.search(r'\d', a):
        return a
    return '금액 비공개(유보)' if '유보' in detail else ''


def _base_type(nm: str) -> str:
    return re.sub(r'\(.*?\)|\[.*?\]', '', nm).strip()


def _fetch_rows(sb, day: datetime.date = None):
    """오늘(KST) 발송된 공시 notice_history — (level, corp, report_nm, rcept) 중복제거.

    19:00 실행 순간의 일시적 네트워크 끊김(httpx 'Server disconnected')으로 그날
    다이제스트가 통째로 누락되던 사례(2026-10-02) 방지 — DB 조회를 3회까지 재시도.
    발송 전 단계라 재시도에 중복 발송 위험 없음.
    """
    import time as _time
    day = day or datetime.datetime.now(_KST).date()
    since = f'{day.isoformat()}T00:00:00+09:00'
    until = f'{(day + datetime.timedelta(days=1)).isoformat()}T00:00:00+09:00'
    res = None
    for attempt in (1, 2, 3):
        try:
            # 시간순 — 예전엔 정렬이 없어 같은 회사·같은 종류 중 어느 건이 남을지 그때그때 달랐다
            res = (sb.table('notice_history').select('target,content')
                   .gte('created_at', since).lt('created_at', until).like('content', '[공시/%')
                   .order('created_at').limit(2000).execute().data or [])
            break
        except Exception as e:
            if attempt == 3:
                log.error(f"[공시 다이제스트] notice_history 조회 3회 실패: {e}")
                raise
            log.warning(f"[공시 다이제스트] notice_history 조회 재시도 {attempt}/2: {e}")
            _time.sleep(2 * attempt)
    seen, items = set(), []
    for x in res:
        m = re.match(r'\[공시/(\w+)\]\s*(.+?)\s*#(\d+)', x.get('content', ''))
        if not m:
            continue
        lvl, nm, rc = m.group(1), re.sub(r'\s+', ' ', m.group(2)).strip(), m.group(3)
        # 같은 회사·같은 종류는 한 건만 — 단 공급계약·수주는 건마다 다른 계약이고(코오롱글로벌 10-26
        # 1,384억·973억 중 하나가 빠졌음), 잠정실적은 별도·연결·자회사 서식을 골라 써야 해서 다 남긴다
        if '공급계약' in nm or '수주' in nm or '잠정' in nm:
            key = (x.get('target', ''), rc)
        else:
            key = (x.get('target', ''), _base_type(nm))
        if key in seen:
            continue
        seen.add(key)
        items.append((lvl, x.get('target', ''), nm, rc))
    return items


def _render_urgent(urgent):
    """긴급 공시 — 유형별 버킷."""
    if not urgent:
        return []
    from dart_parser import get_disclosure_detail
    delist, watch, halt, resume, other = [], [], [], [], []
    for lvl, corp, nm, rc in urgent:
        if '상장적격성' in nm or ('상장폐지' in nm and '안내' in nm):
            outcome = ''
            try:
                d = get_disclosure_detail(rc, nm)
                for ln in d.split('\n'):
                    if ln.startswith('🚨 결과:'):
                        outcome = ln.replace('🚨 결과:', '').strip()
                        break
            except Exception:
                pass
            delist.append(f"{corp} — {outcome or '상장적격성 실질심사'}")
        elif '관리종목' in nm:
            watch.append(corp)
        elif '거래정지해제' in nm.replace(' ', '') or ('거래정지' in nm and '해제' in nm):
            resume.append(corp)
        elif '거래정지' in nm:
            halt.append(corp)
        elif '불성실공시' in nm:
            other.append(f"{corp} — 불성실공시 지정{'예고' if '예고' in nm else ''}")
        elif '횡령' in nm or '배임' in nm:
            other.append(f"{corp} — 횡령·배임")
        elif '공개매수' in nm:
            other.append(f"{corp} — 공개매수")
        elif '회생' in nm or '파산' in nm:
            other.append(f"{corp} — 회생/파산")
        else:
            other.append(f"{corp} — {_base_type(nm)[:20]}")

    _dd = lambda xs: list(dict.fromkeys(xs))
    delist, watch, halt, resume, other = map(_dd, (delist, watch, halt, resume, other))
    lines = ['🚨 <b>긴급</b>']
    for d in delist:
        lines.append(f'• {d}')
    if watch:
        lines.append(f'• 관리종목 지정우려 {len(watch)}곳: {"·".join(watch[:8])}')
    if halt:
        lines.append(f'• 매매거래정지: {"·".join(halt[:8])}')
    if resume:
        lines.append(f'• 거래정지 해제: {"·".join(resume[:8])}')
    for o in other:
        lines.append(f'• {o}')
    return lines


def generate(sb=None, day: datetime.date = None) -> str:
    """저녁 공시 다이제스트 메시지 생성. 발송할 게 없으면 빈 문자열.
    day: 지난 날짜 다시 만들기(검증용) — 기본은 오늘(KST)."""
    if sb is None:
        from supabase_bridge import bridge as _bridge
        sb = _bridge._get_client()
    if not sb:
        return ''

    from dart_parser import get_disclosure_detail
    items = _fetch_rows(sb, day)
    if not items:
        return ''

    n_urg = sum(1 for i in items if i[0] == 'urgent')
    n_maj = sum(1 for i in items if i[0] == 'major')
    n_skip = sum(1 for i in items if i[0] == 'skip')
    if n_urg + n_maj == 0:
        return ''

    urgent = [i for i in items if i[0] == 'urgent']
    earn, contract, subs, gov = OrderedDict(), [], [], []
    misc_increase = []

    # 잠정실적은 회사마다 한 건 — 연결 > 별도 > 자회사(지주가 낸 자회사 실적). 예전엔 먼저 온 서식을 써서
    # 지주사 줄에 자회사 숫자가 들어갈 수 있었다(HD한국조선해양 10-28: 자회사·별도·연결 3건이 1분 안에)
    earn_pick = {}
    for lvl, corp, nm, rc in items:
        if lvl != 'skip' and '잠정' in nm:
            pr = 0 if '자회사' in nm else (2 if '연결' in nm else 1)
            if corp not in earn_pick or pr > earn_pick[corp][0]:
                earn_pick[corp] = (pr, rc)

    for lvl, corp, nm, rc in items:
        if lvl == 'skip':
            continue
        try:
            if '잠정' in nm:
                if earn_pick.get(corp, (0, None))[1] != rc:
                    continue
                d = get_disclosure_detail(rc, nm)
                rev, rev_yoy = _earn_metric(d, '매출')
                op, op_yoy = _earn_metric(d, '영업이익')
                if rev:
                    earn[corp] = (rev, op, op_yoy, rev_yoy, earn_pick[corp][0] == 0)
            elif ('공급계약' in nm or '수주' in nm) and '정정' not in nm and len(contract) < 20:
                d = get_disclosure_detail(rc, nm)
                contract.append((corp, _contract_amount(d), '자회사' in nm))
            elif '청약결과' in nm:
                d = get_disclosure_detail(rc, nm)
                r = _detail_line(d, '청약률')
                subs.append((corp, re.sub(r'.*청약률:\s*', '', r) if r else ''))
            elif '유상증자결정' in nm and '정정' not in nm:
                # 금액 없이 이름만 나가 규모를 알 수 없었다(하이브 10-27) — 조달금액·방식을 붙인다
                d = get_disclosure_detail(rc, nm)
                amt = _field(d, '조달금액') or _field(d, '조달')   # 종속회사 서식은 '조달: 타법인취득자금 …'
                how = _field(d, '방식')
                tail = ' · '.join(x for x in (amt, how) if x)
                who = f'{corp}(종속회사)' if '종속회사' in nm else corp
                misc_increase.append(f'{who} 유상증자' + (f' {tail}' if tail else '결정'))
            elif '정지' not in nm and '정정' not in nm:
                # 지배구조 이벤트 — '분할'은 회사분할만(주식 병합·분할 거래정지 제외),
                # 라벨은 매칭 키워드로(주요사항보고서 등 껍데기 제목 방지)
                for kw in ('합병', '회사분할', '물적분할', '인적분할',
                           '주식소각', '공개매수', '최대주주변경'):
                    if kw in nm:
                        gov.append(f'{corp} {kw}')
                        break
        except Exception:
            log.debug(f'[다이제스트] enrich 실패: {corp} {nm}')

    now = datetime.datetime.combine(day, datetime.time()) if day else datetime.datetime.now(_KST)
    head = (f"🌆 <b>오늘의 공시 요약</b> · {now.month}월 {now.day}일({_WD[now.weekday()]})\n"
            f"════════════\n"          # 다른 텔레그램 메시지와 같은 구분선(═×12)
            f"📮 주요·긴급 <b>{n_urg + n_maj}건</b> (🚨긴급 {n_urg} · 📌주요 {n_maj})")
    blocks = [head]

    u = _render_urgent(urgent)
    if u:
        blocks.append('\n'.join(u))

    if earn:
        # 지주가 낸 자회사 실적은 그 자회사가 따로 낸 실적(같은 매출)이 있으면 뺀다
        own_revs = {v[0] for v in earn.values() if not v[4]}
        rows = sorted(((c, v) for c, v in earn.items() if not (v[4] and v[0] in own_revs)),
                      key=lambda x: -_amt_num(x[1][0]))
        lines = ['📊 <b>잠정실적</b> (매출 / 영업익 · 영업익 YoY)']
        for corp, (rev, op, op_yoy, rev_yoy, is_sub) in rows[:10]:
            name = f'{corp}(자회사)' if is_sub else corp
            if op:
                lines.append(f'• {name}: {rev} / {op}' + (f' · YoY {op_yoy}' if op_yoy else ''))
            else:   # 영업익 칸이 '-'인 공시 — 예전엔 '?'로 나갔다
                lines.append(f'• {name}: {rev} / 영업익 미기재' + (f' · 매출 YoY {rev_yoy}' if rev_yoy else ''))
        if len(rows) > 10:
            lines.append(f'• 외 {len(rows) - 10}곳')
        blocks.append('\n'.join(lines))

    if contract:
        # 계약마다 한 줄(같은 회사 다른 계약도 따로 — 예전엔 회사당 1건만 남아 코오롱글로벌 1,384억이 빠졌음).
        # 같은 회사·같은 금액은 같은 계약의 다른 서식이라 한 줄. 지주가 낸 자회사 계약(…자회사의 주요경영사항)은
        # 자회사 공시(같은 금액)가 따로 있으면 빼고, 없으면 '(자회사)'·'자회사 매출대비'로 — 지주사 줄에 자회사
        # 매출 대비 비율이 그대로 붙어 오해를 샀다(코오롱 3.63%·HL홀딩스 14.58%, 10-26·28)
        _key = lambda a: (a or '').split('(')[0].strip()
        own = {_key(a) for c, a, sub in contract if not sub and re.search(r'\d', _key(a))}
        rows, seen_c = [], set()
        for c, a, sub in contract:
            k = _key(a)
            if sub and k in own:
                continue
            if (c, k) in seen_c:
                continue
            seen_c.add((c, k))
            rows.append((f'{c}(자회사)', (a or '').replace('(매출대비', '(자회사 매출대비')) if sub else (c, a))
        rows.sort(key=lambda x: -_amt_num(x[1]))
        lines = ['🤝 <b>공급계약·수주</b>']
        for corp, a in rows[:8]:
            lines.append(f'• {corp}: {a}' if a else f'• {corp}')
        if len(rows) > 8:
            lines.append(f'• 외 {len(rows) - 8}건')
        blocks.append('\n'.join(lines))

    if subs or misc_increase:
        lines = ['💰 <b>증자·청약</b>']
        for corp, r in subs:
            over = ' (초과청약)' if re.match(r'1[0-9][0-9]', r or '') else ''
            lines.append(f'• {corp} 청약률 {r}{over}' if r else f'• {corp} 청약결과')
        for x in misc_increase:
            lines.append(f'• {x}')
        blocks.append('\n'.join(lines))

    if gov:
        lines = ['🔄 <b>지배구조·주주환원</b>']
        for g in list(dict.fromkeys(gov))[:8]:
            lines.append(f'• {g}')
        blocks.append('\n'.join(lines))

    if n_skip:
        blocks.append(f'<i>※ 잡공시(지분보고·소유상황 등) {n_skip}건 제외 · 상세는 종목 채널</i>')

    return '\n\n'.join(blocks)


if __name__ == '__main__':
    print(generate())
