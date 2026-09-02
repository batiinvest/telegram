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


def _detail_line(detail: str, key: str) -> str:
    for ln in detail.split('\n'):
        head = ln.split(':', 1)[0]
        if key in head:
            return ln.strip()
    return ''


def _base_type(nm: str) -> str:
    return re.sub(r'\(.*?\)|\[.*?\]', '', nm).strip()


def _fetch_rows(sb):
    """오늘(KST) 발송된 공시 notice_history — (level, corp, report_nm, rcept) 중복제거."""
    since = datetime.datetime.now(_KST).strftime('%Y-%m-%d') + 'T00:00:00+09:00'
    res = (sb.table('notice_history').select('target,content')
           .gte('created_at', since).like('content', '[공시/%')
           .limit(2000).execute().data or [])
    seen, items = set(), []
    for x in res:
        m = re.match(r'\[공시/(\w+)\]\s*(.+?)\s*#(\d+)', x.get('content', ''))
        if not m:
            continue
        lvl, nm, rc = m.group(1), re.sub(r'\s+', ' ', m.group(2)).strip(), m.group(3)
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


def generate(sb=None) -> str:
    """저녁 공시 다이제스트 메시지 생성. 발송할 게 없으면 빈 문자열."""
    if sb is None:
        from supabase_bridge import bridge as _bridge
        sb = _bridge._get_client()
    if not sb:
        return ''

    from dart_parser import get_disclosure_detail
    items = _fetch_rows(sb)
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

    for lvl, corp, nm, rc in items:
        if lvl == 'skip':
            continue
        try:
            if '잠정' in nm and corp not in earn:
                d = get_disclosure_detail(rc, nm)
                mr = re.search(r'매출:\s*([\d,조억]+)(?:.*?YoY\s*([+\-\d.]+%))?', d)
                mo = re.search(r'영업이익:\s*([\d,조억]+)', d)
                if mr:
                    earn[corp] = (mr.group(1), mo.group(1) if mo else '?', mr.group(2) or '')
            elif ('공급계약' in nm or '수주' in nm) and '정정' not in nm and len(contract) < 6:
                d = get_disclosure_detail(rc, nm)
                a = _detail_line(d, '계약금액')
                contract.append((corp, re.sub(r'.*계약금액:\s*', '', a) if a else ''))
            elif '청약결과' in nm:
                d = get_disclosure_detail(rc, nm)
                r = _detail_line(d, '청약률')
                subs.append((corp, re.sub(r'.*청약률:\s*', '', r) if r else ''))
            elif '유상증자결정' in nm and '정정' not in nm:
                misc_increase.append(f'{corp} 유상증자결정')
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

    now = datetime.datetime.now(_KST)
    head = (f"🌆 <b>오늘의 공시 요약</b> · {now.month}월 {now.day}일({_WD[now.weekday()]})\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📮 주요·긴급 <b>{n_urg + n_maj}건</b> (🚨긴급 {n_urg} · 📌주요 {n_maj})")
    blocks = [head]

    u = _render_urgent(urgent)
    if u:
        blocks.append('\n'.join(u))

    if earn:
        rows = sorted(earn.items(), key=lambda x: -_amt_num(x[1][0]))
        lines = ['📊 <b>잠정실적</b> (매출 / 영업익 · YoY)']
        for corp, (rev, op, yoy) in rows[:10]:
            y = f' · YoY {yoy}' if yoy else ''
            lines.append(f'• {corp}: {rev} / {op}{y}')
        if len(rows) > 10:
            lines.append(f'• 외 {len(rows) - 10}곳')
        blocks.append('\n'.join(lines))

    if contract:
        # 같은 종목 중복(신고서/정정 등 다른 서식)·빈 금액 제거 — 금액 큰 것 우선
        best = {}
        for _c, _a in contract:
            if _c not in best or _amt_num(_a) > _amt_num(best[_c]):
                best[_c] = _a
        rows = sorted(best.items(), key=lambda x: -_amt_num(x[1]))
        lines = ['🤝 <b>공급계약·수주</b>']
        for corp, a in rows:
            lines.append(f'• {corp}: {a}' if a else f'• {corp}')
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
