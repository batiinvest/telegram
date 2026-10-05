"""
dart_parse_helpers.py — 공시 파서 공용 헬퍼·상수 (2026-07 dart_parsers 분할)

카테고리별 파서(dart_parsers_*.py)가 공유하는 포맷·정제 함수와 상수.
원문 취득·kv 빌드는 dart_doc, 배선은 dart_parser(파사드).
"""
import re
import logging

from dart_doc import (
    _get, _trunc, _trunc_clean,
    _fetch_dart_majorstock, _fetch_dart_reporter,
)

log = logging.getLogger("dart_parsers")   # 분할 전 로거명 유지


def _fmt_amount(v: str) -> str:
    """금액 포맷: '110,758,162,833' → '1,107억'"""
    try:
        n = int(v.replace(',', '').replace(' ', ''))
        if n >= 1_000_000_000_000:
            cho = n // 1_000_000_000_000
            eok = (n % 1_000_000_000_000) // 100_000_000
            return f'{cho}조 {eok:,}억' if eok else f'{cho}조'
        if n >= 100_000_000:
            eok = n // 100_000_000
            return f'{eok:,}억'
        if n >= 10_000:
            return f'{n:,}'
        return str(n)
    except (ValueError, AttributeError):
        return v


def _f(lines: list, kv: dict, label: str, *keys,
       fmt=None, suffix: str = '', trunc: int = 0):
    """_get() + lines.append() 두 줄 패턴을 한 줄로 압축.
    Returns the extracted value (or None)."""
    v = _get(kv, *keys)
    if v:
        if trunc:
            v = _trunc(v, trunc)
        lines.append(f'{label}: {fmt(v) if fmt else v}{suffix}')
    return v


_CI_METHOD = {
    '1': '주주배정', '2': '주주우선공모', '3': '일반공모',
    '4': '직원배정', '5': '일반공모+주주배정', '6': '제3자배정', '7': '기타',
}


_FUND_KEYS = [
    'Facility investment', 'Operating capital (KRW)',
    'Acquiring other companies', 'Debt repayment',
    'Operating capital', 'Other',
]


def _is_footnote(val: str) -> bool:
    """값이 각주/부연설명 텍스트인지 판단.
    '1. 적용환율', '2. 동 계약' 등 번호+설명으로 시작하고 100자 초과인 경우."""
    if not val or len(val) < 30:
        return False
    return bool(re.match(r'^\d+\s*[.）)]\s*\S', val.strip())) and len(val) > 80


def _clean_party(raw: str) -> str:
    """계약상대방 값에서 주소·주석 제거 후 업체명만 반환."""
    if not raw:
        return raw
    # ' - 상기...' 주석 제거
    first_line = raw.split(' - ')[0].strip()
    if first_line and not first_line.startswith('-'):
        # 업체명 뒤 주소 괄호 제거: '한국동서발전(주) (제주특별자치도...)' → '한국동서발전(주)'
        # 단, 짧은 괄호(약칭·코드)는 유지 — 길이 15자 초과 괄호만 제거
        name = re.sub(r'\s*\([^)]{15,}\)', '', first_line).strip()
        return (name or first_line)[:60]
    # 값 전체가 주석으로 시작 — 영문 업체명 패턴 추출 시도
    m = re.search(r'([A-Z][A-Za-z0-9\s\(\)]+(?:Co\.|Corp\.|Ltd\.|LLC|Inc\.|Board|Project|Power|Plant|Vietnam|Korea|China|Japan|USA)[A-Za-z0-9\s\(\)]*)', raw)
    if m:
        return m.group(1).strip()[:60]
    return '미상'


def _clean_date(raw: str) -> str:
    """날짜 값에서 날짜 패턴만 추출. 참고사항이 붙어있으면 제거."""
    if not raw:
        return raw
    # YYYY-MM-DD 또는 YYYY/MM/DD 패턴 추출
    m = re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', raw)
    if m:
        return m.group(0)
    # '미확정', '협의중', '미정' 키워드 포함 시
    if any(kw in raw for kw in ['협의', '미확정', '미정', '미결정', '추후']):
        return '미정'
    # 숫자로만 이루어진 날짜(YYYYMMDD)
    m2 = re.search(r'\d{8}', raw)
    if m2:
        d = m2.group(0)
        return f'{d[:4]}-{d[4:6]}-{d[6:]}'
    return raw[:20] if len(raw) > 20 else raw


def _clean_ratio(raw: str) -> str:
    """매출액대비(%) 값 정리.
    기재정정 시 '16.02 21.92' 형태로 정정전/후 두 값이 붙는 경우 처리."""
    if not raw:
        return raw
    # 숫자만 추출
    nums = re.findall(r'\d+(?:\.\d+)?', raw)
    if len(nums) >= 2:
        # 두 값이면 정정전 → 정정후 형식으로 표시
        return f'{nums[0]}% → {nums[-1]}%'
    if nums:
        return f'{nums[0]}%'
    return raw


# 지급조건 라벨: '(1차 선급금)' '[대금 지급]' — 한글이 든 괄호만.
# 금액 뒤 '(30%)' 같은 순수 수치 괄호는 라벨이 아니므로 제외된다.
_PAY_LABEL_RE = re.compile(r'[(\[]\s*([^)\]]*[가-힣][^)\]]*?)\s*[)\]]')


def _split_by_pay_label(text: str) -> list[str]:
    """'(1차 선급금) 내용 (잔금) 내용' / '[선급금 수령] O 내용' → ['1차 선급금: 내용', ...]"""
    ms = list(_PAY_LABEL_RE.finditer(text))
    if len(ms) < 2:
        return []
    out = []
    for i, m in enumerate(ms):
        stop = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = re.sub(r'^[○ㅇ·•\-]\s*', '', text[m.end():stop].strip()).strip()
        if body:
            out.append(f'{m.group(1).strip()}: {body}')
    return out


def _split_pay_section(sec: str) -> list[str]:
    """지급조건 한 섹션을 하위 항목 줄들로 분해."""
    # (1) 'N)' 하위 항목 → '제목: a / b / c' 한 줄
    sub = re.split(r'(?<!\d)(\d{1,2})\)\s+', sec)
    if len(sub) >= 3:
        title = sub[0].strip().rstrip(':：').strip()
        items = []
        for k in range(1, len(sub) - 1, 2):
            c = re.sub(r'\s+', ' ', sub[k + 1]).strip()
            m = re.match(r'^(.{1,15}?)\s*[:：]\s*(.+)', c)
            items.append(f'{m.group(1).strip()} {m.group(2).strip()}' if m else c)
        joined = ' / '.join(items)
        return [f'{title}: {joined}' if title else joined]

    # (2) '(라벨)'·'[라벨]' 경계
    labeled = _split_by_pay_label(sec)
    if labeled:
        return labeled

    # (3) ' - ' 목록: '라벨:값' 뒤의 무라벨 항목은 그 지급조건이므로 같은 줄로 병합
    dash = [re.sub(r'^[-·•○]\s*', '', s).strip()
            for s in re.split(r'\s+-\s+', sec) if s.strip()]
    dash = [d for d in dash if d]
    if len(dash) > 1:
        merged: list[str] = []
        for d in dash:
            if not merged or re.match(r'^[^:：]{1,14}[:：]', d):
                merged.append(d)
            else:
                merged[-1] += f' — {d}'
        # 선두가 수치·라벨 없는 짧은 머리말('대금지급' 등)이면 제거 — 상위 '지급조건'과 중복
        if len(merged) > 1 and len(merged[0]) <= 8 and not re.search(r'[:：\d]', merged[0]):
            merged = merged[1:]
        return merged

    return [re.sub(r'^[-·•○]\s*', '', sec).strip()]


def _fmt_payment_terms(raw: str, skip_text: str = '',
                       max_lines: int = 10, line_limit: int = 200) -> list[str]:
    """지급조건 텍스트를 줄 단위 목록으로 변환.

    DART 지급조건은 서식이 제각각이라 네 구조를 모두 처리한다:
      '1. 기자재비 1) 선급금: 20%~50%'              → 번호 섹션 + N) 하위항목
      '(1차 선급금) ... (잔금) ...'                  → 괄호 라벨
      '[선급금 수령] O ... [대금 지급] O ...'        → 대괄호 라벨 + 불릿
      '대금지급 - 선급금:1,347원(30%) - 계약체결 후...' → 대시 목록(값+조건 병합)

    지급 스케줄은 투자판단에 쓰이는 정보라 줄당 line_limit(200자)까지 넉넉히 표시해
    중간 절단을 최소화한다. skip_text(계약명)와 같은 섹션은 중복이라 제외.
    """
    text = re.sub(r'\s+', ' ', raw or '').strip()
    if not text:
        return []

    # 최상위 번호 섹션 (1. 2. 3. …)
    top = re.split(r'(?<!\d)(\d{1,2})\.\s+', text)
    sections = [top[i + 1].strip() for i in range(1, len(top) - 1, 2)]

    # 섹션들이 공통 접두(계약명 등)를 반복하면 제거 — 예 '누리호 FM7 …' / '누리호 FM8 …'
    if len(sections) >= 2:
        _lo, _hi = min(sections), max(sections)
        _n = 0
        while _n < len(_lo) and _lo[_n] == _hi[_n]:
            _n += 1
        _pre = _lo[:_n]
        _pre = _pre[:_pre.rfind(' ') + 1] if ' ' in _pre else ''   # 단어 경계까지
        if len(_pre.strip()) >= 6:
            sections = [s[len(_pre):].lstrip() for s in sections]

    entries: list[str] = []
    for sec in (sections or [text]):
        entries.extend(_split_pay_section(sec))

    # 계약명을 그대로 반복하는 섹션 제거(앞 30자 비교)
    if skip_text:
        key = re.sub(r'\s+', '', skip_text)[:30]
        entries = [e for e in entries if re.sub(r'\s+', '', e)[:30] != key]

    return [f'  • {_trunc_clean(e, line_limit)}'
            for e in entries[:max_lines] if e.strip()]


def _strip_disclaimer(text: str) -> str:
    """※ 투자유의사항 면책 문구 제거 (주요내용 앞부분).

    전략:
    1. '상존합니다' 뒤에 실제 내용이 있으면 그 이후만 반환
    2. 없으면 빈 문자열 반환 (제목으로 충분)
    """
    if not text.startswith('※'):
        return text

    # 면책 종결 패턴들: 이후 내용 추출
    _ENDS = [
        r'상존합니다[.。]?\s*',
        r'해지될 수 있습니다[.。]?\s*',
        r'바랍니다[.。]?\s*',
        r'유의하시기 바랍니다[.。]?\s*',
    ]
    for pat in _ENDS:
        m = re.search(pat, text)
        if m:
            rest = text[m.end():].strip()
            # 2차 면책문구 제거: '투자자는 수시공시... 바랍니다.' 패턴
            rest = re.sub(r'^투자자는\s+수시공시.*?바랍니다[.。]?\s*', '', rest, flags=re.DOTALL).strip()
            if rest:
                return rest

    # 면책 종결 없어도 번호 목록(1. / 1) 패턴) 시작점이 있으면 거기부터 반환
    m2 = re.search(r'(?<!\d)(?:1[.)] |\(1\) )', text)
    if m2 and m2.start() > 0:
        return text[m2.start():].strip()

    # ※로 시작하지만 실제 내용 찾을 수 없음
    return ''


def _get_body(kv: dict, *keys: str) -> str | None:
    """서술형 본문 필드 조회 — [기재정정]에서 '정정후_' 값은 바뀐 문장 조각뿐인 경우가
    많아(세븐브로이 2026-10-02: 정정후=3번 항목만) 정정이 반영된 전문(접두어 없는 키)을 우선.
    전문이 정정후 조각을 포함할 때만 채택 — 정정표 패턴 A처럼 접두어 없는 키에 구 값이
    남는 양식에서 구 값을 오채택하지 않도록. 해당 없으면 _get과 동일."""
    frag = _get(kv, *keys)
    if not frag:
        return frag
    nf = re.sub(r'\s+', '', frag)
    for key in keys:
        for k, v in kv.items():
            if key in k and '정정전' not in k and '정정후' not in k:
                full = re.sub(r'\s+', ' ', v or '').strip()
                if len(full) > len(frag) and nf in re.sub(r'\s+', '', full):
                    return full
    return frag


_DIFF_SPLIT = re.compile(
    # 번호('1. '·'2) ')·한글 순번('가. ')·'※' — 앞 공백 직전이 '숫자.'면 제외('2026. 9. 13.' 날짜 보호)
    r'(?:^|(?<!\d\.)\s+)(?:-\s*)?(?:\d{1,2}[.)）](?!\d)|[가나다라마바사아자차카타파하][.)]|※\.?)\s*'
    # 공백 없이 붙은 번호('…중요사항1) 합병…', '…입니다.2) 상기…') — 띄어 쓴 쪽과 분절 기준을 맞춤
    r'|(?<=[가-힣])\d{1,2}\)\s*|(?<=[가-힣]\.)\d{1,2}\)\s*'
    # 대시 불릿 — 뒤가 공백·한글·괄호일 때만('-\'주식수' 표 셀·'-18회차' 보호), 콜론 뒤 값 대시는 제외
    r'|(?:^|(?<!:)\s+)-(?=\s|[가-힣(㈜])\s*'
    # 문장 끝
    r'|(?<=[다음함됨임]\.)\s+'
    # 담보·보증 내역 '[순번 N]' — 순번이 밀려도 같은 계약은 같은 조각(번호는 비교에서 제외)
    r'|\s*\[\s*순번\s*\d+\s*\]\s*')


def _diff_segments(text: str) -> list[str]:
    """정정 비교용 분절 — 번호·순번·대시 불릿·문장끝 모두 경계(정정전/후 분절 기준 일치).
    표지·대시만 남은 빈 조각('가. - 나. -', '-', '다 음')과 중복 조각은 버림."""
    t = re.sub(r'\s+', ' ', text or '').strip()
    segs, seen = [], set()
    for p in _DIFF_SPLIT.split(t):
        s = (p or '').strip(' -')
        k = re.sub(r'[\s.。]', '', s)
        if len(re.sub(r'[^가-힣A-Za-z0-9]', '', s)) < 3 or k in seen:   # '다 음'·'가.' 잔여 제외
            continue
        seen.add(k)
        segs.append(s)
    return segs


def _prose_diff(field: str, old: str, new: str, full: str = '', max_seg: int = 4,
                seg_len: int = 250) -> str:
    """긴 서술형 정정 → 바뀐 항목/문장만 '[전]'/'[후]' 줄로(공통 문장은 생략).
    'old → new' 한 줄(양쪽 60자 절단)로는 무엇이 바뀌었는지 알 수 없던 문제 대응.

    full: 정정 반영 전문(접두어 없는 키). 정정후가 바뀐 문장 조각뿐이고 정정전은 구 전문인
    양식(세븐브로이 2026-10-02)에선 조각과 비교하면 안 바뀐 항목이 [전]으로 오표시됨 →
    '정정전에 있고 조각엔 없는 문장이 전문에 남아 있을' 때만 전문과 비교. 정정전도 조각인
    양식(한미약품 2026-08-26)에서 전문과 비교하면 안 바뀐 항목이 [후]로 쏟아지므로 조각 유지.
    차이가 공백·마침표뿐이면 ''."""
    def norm(s):
        return re.sub(r'[\s.。]', '', s)
    o, n = _diff_segments(old), _diff_segments(new)
    on, nn = {norm(s) for s in o}, {norm(s) for s in n}
    if full and len(full) > len(new) and norm(new) in norm(full):
        fs = _diff_segments(full)
        if any(norm(s) in on and norm(s) not in nn for s in fs):
            n, nn = fs, {norm(s) for s in fs}
    # 공백·숫자·콜론 없는 12자 이하 단일 토큰('임상시험명칭'·'계약상대방'·'회사명'·'가~라')은
    # 값이 아닌 항목 머리말 — 정정 [전]/[후]에 단독으로 뜨면 소음
    def bare(s):
        return len(s) <= 12 and not re.search(r'[\d\s:：]', s)
    rem = [s for s in o if norm(s) not in nn and not bare(s)]
    add = [s for s in n if norm(s) not in on and not bare(s)]
    if not rem and not add:
        return ''
    out = [f'🔧 {field or "변경"}:']
    for tag, segs in (('전', rem), ('후', add)):
        out += [f'    [{tag}] {_trunc_clean(s, seg_len)}' for s in segs[:max_seg]]
        if len(segs) > max_seg:
            out.append(f'    [{tag}] …외 {len(segs) - max_seg}건')
    return '\n'.join(out)


_REL_MARK = re.compile(r'(?:※|-|\d\.)?\s*관련\s*공시\s*-?\s*(?=\d{4}[.-]\d{2}[.-]\d{2})')
_ETC_BOILER = re.compile(r'(?:상기\s*)?결정\S*\s*일자는.{0,40}?이사회\s*(?:결의일|개최일)'
                         r'|관련\s*공시를?\s*참(?:조|고)하시기\s*바랍니다')


def _etc_segments(text: str) -> list[str]:
    """'기타 투자판단 참고사항' 서술 → 항목 목록('- '·'1. '·'가. ' 구분, 4자 미만·'-' 제외)."""
    t = re.sub(r'\s+', ' ', text or '').strip()
    if not t or t in ('-', '해당사항 없음', '해당없음', '없음'):
        return []
    parts = re.split(r'(?:^|\s+)(?:[-·•●○▶■□]|(?<![\w.])\d{1,2}[.)](?!\d)|[가나다라마바사아][.)])\s*', t)
    segs = [p.strip(' -') for p in parts if p]
    return [s for s in segs if len(s) >= 4]


def _related_list(text: str, n: int = 2) -> str:
    """관련공시 목록 텍스트 → 최신 n건 '날짜 제목 · 날짜 제목'.
    날짜 표기 '2026.01.19'·'2026-01-19' 모두, 구분자(' - '·공백) 무관, 날짜순 정렬."""
    hits = list(re.finditer(r'(\d{4})[.-](\d{2})[.-](\d{2})\.?', text or ''))
    out, seen = [], set()
    for j, m in enumerate(hits):
        end = hits[j + 1].start() if j + 1 < len(hits) else len(text)
        title = re.sub(r'\s+', ' ', text[m.end():end]).strip(' -·,')
        if len(title) > 45:   # '…사항(임상시험 계획 승인신청)(전이성 …)' → 첫 괄호까지
            title = re.sub(r'(\([^()]*\))\s*\(.*$', r'\1', title)
        key = (m.group(1) + m.group(2) + m.group(3), title)
        if title and key not in seen:
            seen.add(key)
            out.append((key[0], m.group(0).rstrip('.'), title))
    out.sort(key=lambda x: x[0])
    return ' · '.join(f'{d} {_trunc_clean(t, 45)}' for _, d, t in out[-n:])


def _rel_text(v: str, n: int = 3) -> str:
    """'🔗 관련' 값 — 최신 n건 '날짜 제목'(날짜 없는 서식은 문장경계 110자)."""
    return _related_list(v, n) or _trunc_clean(re.sub(r'\s+', ' ', v or ''), 110)


_LETTER_SEQS = ('abcdefgh', '가나다라마바사아')


def _split_lettered(text: str):
    """'항목명 a. 내용 b. 내용 …' / '가. … 나. …' → (항목명, [하위줄…]), 해당 없으면 None.

    순번이 첫 글자부터 2개 이상 연속(a→b, 가→나)일 때만 인정 — 'U.S.A.'·'e.g.'·
    '참가.'·'(a)' 같은 문장 속 표기 오인 방지. 마지막 항목 꼬리의 '※ 주석'·'→/-> 비고'는
    별도 줄로 분리(계약금액 비공개 안내 등이 마지막 순번에 붙어 뭉치던 것).
    """
    flat = re.sub(r'\s+', ' ', text).strip()
    for seq in _LETTER_SEQS:
        picked = []
        for m in re.finditer(rf'(?<![\w.(])([{seq}])[.)]\s+', flat):
            if len(picked) < len(seq) and m.group(1) == seq[len(picked)]:
                picked.append(m)
        if len(picked) < 2:
            continue
        lead = flat[:picked[0].start()].strip().rstrip(':：-').strip()
        subs = []
        for j, m in enumerate(picked):
            end = picked[j + 1].start() if j + 1 < len(picked) else len(flat)
            body = flat[m.end():end].strip().rstrip('-').strip()
            subs.append(f'{m.group(0).strip()} {body}')
        tail = re.split(r'\s+(?=※|→|->)', subs[-1])
        subs[-1:] = [t.strip() for t in tail if t.strip()]
        return lead, subs
    return None


def _parse_numbered_body(text: str, max_items: int = 8, val_limit: int = 300) -> list[str]:
    """'1) 항목명: 내용' / '1. 항목명 - 내용' 형태 번호 목록을 줄별 bullet로 변환.

    - 값 선두의 대시 불릿('- ')은 노이즈라 제거.
    - 값 안에 ' - ' 하위항목이 여럿이면(예: 신청일/승인일/조기종료일/승인기관)
      **버리지 않고** 개행+들여쓰기로 모두 표시(핵심 날짜·기관 보존).
    - 서술형(사유·향후계획 등)은 val_limit까지 넉넉히 표시(핵심 정보라 절단 최소화).
    """
    # 번호 목록 분리: '1)' / '1. ' / '1.임상'(공백없음) 모두 지원.
    # (?!\d): '0.56'·날짜('06.30')·소수는 분리 안 함. (?<!\w): '제3상'·'GV1001' 보호.
    # [.)）]: 반각 '.'·')' + 전각 '）'(일부 공시가 '3）'처럼 전각 사용).
    # (?<![\w".“”]): 앞이 단어문자·소수점·인용부호면 분리 안 함 — '상기 "2) 적응증"'
    # 문장 내 번호 참조, 'USD 415,729.61)'의 소수부 '61)'을 새 항목으로 오분리하던 문제 방지.
    parts = re.split(r'\s*(?<![\w".“”])(\d{1,2})[.)）](?!\d)\s*', text)
    # parts = ['prefix', '1', 'content1 ', '2', 'content2 ', ...]
    items = []
    i = 1
    while i < len(parts) - 1:
        content = parts[i + 1].strip()
        # 원문자 하위섹션(①②③…) 분리 — '임상시험 관련 사항 ①승인일…②등록번호…③경과…
        # ④결과…'가 한 줄로 뭉치고 뒤가 잘리던 문제. 선두(항목명) + 원문자별
        # '마커 헤더: 값 / 값' 하위줄을 하나의 멀티라인 항목으로(max_items 카운팅 정합).
        circ = re.split(r'\s*([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮])\s*', content)
        if len(circ) >= 3:
            lead = re.sub(r'\s+', ' ', circ[0]).strip().rstrip(':').strip()
            sub_lines = []
            for j in range(1, len(circ) - 1, 2):
                seg = re.sub(r'\s+', ' ', circ[j + 1]).strip()
                seg_subs = [s.strip() for s in re.split(r'\s+-\s+', seg) if s.strip()]
                if len(seg_subs) >= 2:
                    hd = seg_subs[0].rstrip(':').strip()
                    body = ' / '.join(_trunc_clean(s, 130) for s in seg_subs[1:5])
                    sub_lines.append(f'      {circ[j]} {hd}: {body}')
                elif re.search(r'\[\s*[\d,]+\s*\]\s*억.*?\[\s*[\d,]+\s*\]\s*억', seg):
                    # 마일스톤 표('… [ N ]억 원' 2회↑ 반복) → 단계별 줄바꿈 + '[ 10 ]'→'10'.
                    _s = re.sub(r'\[\s*([\d,]+)\s*\]', r'\1', seg)
                    ms = [m.strip() for m in re.split(r'(?<=억 원)\s+', _s) if m.strip()]
                    sub_lines.append(f'      {circ[j]} {_trunc_clean(ms[0], 100)}')
                    for m in ms[1:8]:
                        sub_lines.append(f'          - {_trunc_clean(m, 90)}')
                elif seg:
                    sub_lines.append(f'      {circ[j]} {_trunc_clean(seg, 350)}')
            if sub_lines:
                items.append((f'  • {lead}\n' if lead else '  • ') + '\n'.join(sub_lines))
                i += 2
                if len(items) >= max_items:
                    break
                continue
        # 순번 하위목록(a. b. c. / 가. 나. 다.) — '계약 금액 a. 계약금: … b. 공동연구 대가: …'가
        # 아래 key/value 분리에서 첫 콜론('…a. 계약의 대가(계약금):')으로 잘못 갈려 a~d가
        # 한 줄로 뭉치던 문제. 항목명 헤더 + 순번별 하위줄(멀티라인 1항목, max_items 정합).
        if lettered := _split_lettered(content):
            lead, subs = lettered
            if lead:
                head = f'  • {lead}:' if len(lead) <= 40 else f'  • {_trunc_clean(lead, val_limit)}'
            else:
                head, subs = f'  • {_trunc_clean(subs[0], 200)}', subs[1:]
            items.append('\n'.join([head] + [f'      {_trunc_clean(s, 200)}' for s in subs[:8]]))
            i += 2
            if len(items) >= max_items:
                break
            continue
        # 'key: value' 또는 'key - value' 분리 (콜론이 먼저 오면 콜론 우선 매칭)
        m = re.match(r'^(.{1,40}?)\s*[:－-]\s*(.+)', content, re.DOTALL)
        if m:
            key = m.group(1).strip()
            val = re.sub(r'\s+', ' ', m.group(2)).strip()
            val = re.sub(r'^[-·•]\s*', '', val)   # 선두 대시 불릿 제거
            if len(val) < 2 or val in ('없음', '-', '해당없음', '.'):
                i += 2
                continue
            # ' - ' 하위항목 다수 → 개행 정렬, 아니면 단일값 표시
            subs = [s.strip() for s in re.split(r'\s+-\s+', val) if s.strip()]
            if len(subs) >= 2:
                body = '\n      ' + '\n      '.join(_trunc_clean(s, 120) for s in subs[:6])
                items.append(f'  • {key}:{body}')
            else:
                items.append(f'  • {key}: {_trunc_clean(val, val_limit)}')
        else:
            short = re.sub(r'\s+', ' ', content).strip()
            # 선두 문장 + ' - ' 나열(대상/조건/예외 목록)이 있으면 개행 정렬 —
            # 'KBO리그 경기 - 신인드래프트 - 단, IPTV 제외'류가 한 줄로 뭉치는 것 방지.
            _subs = [s.strip() for s in re.split(r'\s+-\s+', short) if s.strip()]
            if len(_subs) >= 3:
                items.append(f'  • {_trunc_clean(_subs[0], val_limit)}')
                for _s in _subs[1:7]:
                    items.append(f'      - {_trunc_clean(_s, 140)}')
            # 단순 섹션 헤더(10자 미만)만 생략 — 예전엔 val_limit 초과 서술 항목도
            # 통째로 빠졌음(조용한 누락). 길면 문장경계 절단으로라도 표시.
            elif len(short) >= 10:
                items.append(f'  • {_trunc_clean(short, max(val_limit, 600))}')
        i += 2
        if len(items) >= max_items:
            break
    return items


def _numbered_with_lead(text: str, max_items: int = 8, val_limit: int = 300) -> list:
    """_parse_numbered_body + 첫 번호 앞 서두를 첫 줄로 보존. _parse_numbered_body는 서두를
    버려 '조회결과 공시후 30분 경과시점까지 단, 1) … 2) …'의 핵심 조건이 사라지던 문제."""
    b = _parse_numbered_body(text, max_items=max_items, val_limit=val_limit)
    if not b:
        return b
    lead = re.split(r'\s*(?<![\w".“”])\d{1,2}[.)）](?!\d)', text, maxsplit=1)[0].strip(' ,-')
    if len(lead) >= 4:
        b.insert(0, f'  • {_trunc_clean(lead, val_limit)}')
    return b


def _clinical_bullet(label: str, content: str, sec_limit: int) -> str:
    """임상 결과 섹션 1개를 bullet로 포맷 (라벨 정리 + 용량행 세로정렬)."""
    label = re.sub(r'\s*\([^)]*\)\s*$', '', label).strip()   # 라벨 뒤 영문 괄호 제거
    content = re.sub(r'^-\s*', '', content).strip()          # 선두 대시 불릿 제거
    content = _trunc_clean(content, sec_limit)
    # 다중 하위 불릿(' - ') → 개행 정렬: 지표별(KOOS/WOMAC/VAS…) 세로 나열로 스캔 용이.
    # (선두 '- '는 위에서 제거됨 → 문장 사이 ' - '만 대상. 'TG-C' 등 공백없는 하이픈 무관)
    content = re.sub(r'\s+-\s+(?=\S)', '\n      - ', content)
    # 용량행·위약 앞 개행 → 용량반응/약동학 표를 세로 정렬.
    # 단 콤마·여는괄호 뒤(문장 내 인라인 '(TG-C: x, 위약: y)')는 제외 — 통계 서술을
    # 괄호 중간에서 끊지 않도록. 실제 표 행(직전이 값·글자로 끝남)에서만 개행.
    content = re.sub(r'(?<![,(\s])\s+(?=(?:\d[\d,]*\s*mg|위약)\s*:)', '\n      ', content)
    return f'  • {label}: {content}'


def _parse_clinical_result(text: str, max_sections: int = 5, sec_limit: int = 600) -> list:
    """임상시험결과 '결과값'을 섹션 단위로 분리해 bullet로 반환.

    세 서식 지원:
      ①  '[1차 평가변수(Co-Primary Endpoint)] ... [안전성(Safety)] ...'  (대괄호 헤더)
      ②  '1. 안전성 평가변수 … 2. 유효성 평가변수 …'                     (최상위 번호)
      ③  '- 항바이러스 활성: ... - 안전성, 내약성: ...'                    (대시 콜론)
    핵심 결론(유효성 입증 여부 등)이 섹션 첫머리로 올라와 통짜 truncate 폴백보다
    가독성이 크게 개선됨. 셋 다 실패 시 빈 리스트(호출측 fallback).
    본문 내 인라인 콜론('200 mg:', 'Dose:')·소수점·하위번호('N)')는 오분리되지 않음.
    """
    text = re.sub(r'\s+', ' ', text).strip()

    # ── ① 대괄호 헤더: 텍스트가 '[섹션]'으로 시작할 때만 (인라인 [주] 오분리 방지) ──
    br = re.split(r'\[([^\]]{2,40})\]\s*', text)
    if len(br) >= 3 and len(br[0].strip()) < 10:
        lines = []
        for i in range(1, len(br), 2):
            content = br[i + 1].strip() if i + 1 < len(br) else ''
            if not content:
                continue
            lines.append(_clinical_bullet(br[i], content, sec_limit))
            if len(lines) >= max_sections:
                break
        if lines:
            return lines

    # ── ② 최상위 번호 섹션 'N. 섹션명' (점+공백+한글 → 소수점·하위 'N)'과 구분) ──
    num = re.split(r'(?:^|\s)([1-9])\.\s+(?=[가-힣])', text)
    if len(num) >= 5 and len(num[0].strip()) < 6:
        lines = []
        for i in range(2, len(num), 2):        # 내용은 짝수 인덱스(2,4,…), 앞은 번호
            seg = num[i].strip()
            # 라벨 = 첫 하위마커(N)·①-⑳·'- ') 전까지의 섹션명
            mlab = re.match(r'^([가-힣][가-힣()\s]{1,18}?)(?=\s*(?:\d[).]|[①-⑳]|-\s))', seg)
            label = mlab.group(1).strip() if mlab else _trunc(seg, 15)
            # 본문 = 첫 대시 불릿부터(하위 번호·라벨 노이즈 생략), 없으면 라벨 이후 전체
            rest = seg[mlab.end():] if mlab else seg
            mbody = re.search(r'-\s+(\S.*)', rest, re.DOTALL)
            body = (mbody.group(1) if mbody else rest).strip()
            if len(body) < 5:
                continue
            lines.append(_clinical_bullet(label, body, sec_limit))
            if len(lines) >= max_sections:
                break
        if lines:
            return lines

    # ── ③ 대시 콜론 헤더 ──
    parts = re.split(r'(?:^|\s)-\s+([가-힣][가-힣,·\s]{0,14}):\s+', text)
    if len(parts) < 3:   # 섹션 헤더 못 찾음
        return []
    lines = []
    for i in range(1, len(parts), 2):
        content = parts[i + 1].strip() if i + 1 < len(parts) else ''
        if not content:
            continue
        lines.append(_clinical_bullet(parts[i], content, sec_limit))
        if len(lines) >= max_sections:
            break
    return lines


_BOND_METHOD = {'1': '공모', '2': '사모', '3': '주주배정', '4': '기타'}


def _parse_etc_field(text: str) -> list[str]:
    """'5.기타' 필드의 '-항목 : 값' 목록을 줄별로 분리."""
    lines = []
    for part in re.split(r'\s*-(?=\S)', text.strip()):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'^(.+?)\s*:\s*(.+)$', part)
        if m:
            lines.append(f'  • {m.group(1).strip()}: {m.group(2).strip()}')
        else:
            lines.append(f'  • {_trunc(part, 70)}')
    return lines


def _clean_amendment_field(field: str) -> str:
    """기재정정 필드명 정리 — 섹션경로 제거 후 핵심 필드명만 반환."""
    f = field.strip()
    # 'N. 섹션명' 앞 번호 제거
    f = re.sub(r'^\d+\.\s*', '', f)
    # '[섹션명]' 대괄호 제거
    f = re.sub(r'^\[.+?\]\s*', '', f)
    # ' -' 구분자로 분리 (뒤 공백 유무 관계없이)
    parts = [p.strip() for p in re.split(r'\s+-\s*', f) if p.strip()]
    f = parts[-1] if parts else f
    # 괄호 단위 제거: '체결일(당해건)', '지분율(%)' → 핵심어만
    f = re.sub(r'\s*\([^)]{1,10}\)\s*$', '', f).strip()
    # 마지막 의미있는 한글 단어 추출 (공백 분리 후 뒤에서 탐색)
    words = f.split()
    for w in reversed(words):
        w_core = re.sub(r'[()%주건원,.]', '', w)
        if re.search(r'[가-힣]{2,}', w_core):
            f = re.sub(r'\s*\([^)]{1,10}\)\s*$', '', w).strip()
            break
    return _trunc(f, 15)


def _fmt_amendment_val(field_name: str, val: str) -> str:
    """기재정정 비교값 포맷 — 금액/날짜/비율 필드에 맞게 변환."""
    if not val or val in ('-', '—', '없음', 'N/A'):
        return val
    # 비율 필드 — '- 계약금액:... - 매출액대비 : 70.12' 복합값에서 비율만 추출
    if any(kw in field_name for kw in ('대비', '비율', '%', '비중')):
        m = re.search(r'대비\s*[：:]\s*([\d.]+)', val)
        if m:
            return m.group(1) + '%'
        nums = re.findall(r'\d+(?:\.\d+)?', val)
        if nums:
            return nums[-1] + '%'
    # 금액 필드 — '- 계약금액: 141987535126' 복합값에서 숫자만 추출
    if any(kw in field_name for kw in ('금액', '가격', '대금', '보증금')):
        m = re.search(r'([\d,]{5,})', val)
        if m:
            try:
                return _fmt_amount(m.group(1)) + '원'
            except Exception:
                pass
        try:
            return _fmt_amount(val) + '원'
        except Exception:
            pass
    # 날짜 필드
    if any(kw in field_name for kw in ('일', '기간', '시작', '종료')):
        cleaned = _clean_date(val)
        if cleaned != val:
            return cleaned
    return _trunc(val, 40)


def _parse_agm_notice_text(kv: dict) -> list:
    """주주총회소집공고 자유서식 본문 '아래' 섹션(일시·장소·보고·부의안건) 파싱.

    KV 테이블은 이사회 결의이력·참석표가 뒤섞여 범용 파서로는 난독이므로,
    규격화된 소집공고 본문(1. 일시 : ... 2. 장소 : ... N. 부의 안건 : 제1호...)을
    원문 텍스트에서 직접 추출. 섹션 종결자는 다음 번호 헤더(' N. 한글')로 일반화.
    """
    lines = []
    txt = re.sub(r'<[^>]+>', ' ', kv.get('_html', ''))
    txt = re.sub(r'\s+', ' ', txt).strip()

    def _sec(label_pat: str) -> str:
        m = re.search(label_pat + r'\s*[:：]\s*(.+?)\s+\d+\s*\.\s*[가-힣]', txt)
        return m.group(1).strip() if m else ''

    # 회차 (제N기 임시/정기 주주총회)
    m_round = re.search(r'(제\s*\d+\s*기\s*(?:임시|정기)?\s*주주총회)', txt)
    if m_round:
        lines.append('🏛 ' + re.sub(r'\s+', ' ', m_round.group(1)).strip())

    if dt := _sec(r'일\s*시'):
        lines.append(f'📅 일시: {_trunc(dt, 50)}')
    if loc := _sec(r'장\s*소'):
        lines.append(f'📍 장소: {_trunc(loc, 60)}')
    if rpt := _sec(r'보고사항'):
        lines.append(f'📢 보고: {_trunc(rpt, 50)}')

    # 부의 안건 — '제N호' 단위 분리
    m_ag = re.search(
        r'(?:부의\s*안건|회의의?\s*목적사항?|회의목적)\s*[:：]\s*(.+?)\s+\d+\s*\.\s*[가-힣]', txt)
    if m_ag:
        items = [it.strip() for it in re.split(r'\s*제\s*\d+\s*호\s*', m_ag.group(1)) if it.strip()]
        if items:
            lines.append('📋 부의 안건:')
            for idx, it in enumerate(items[:10], 1):
                lines.append(f'  제{idx}호. {_trunc(it, 70)}')

    # 구조 미매칭 시 범용파서(난독 테이블) fallback 방지 — 헤더성 한 줄로 대체
    if not lines:
        lines.append('🏛 주주총회 소집 — 안건은 공시 원문 참조')

    return lines


__all__ = ['log', '_get', '_trunc', '_trunc_clean', '_fetch_dart_majorstock', '_fetch_dart_reporter', '_fmt_amount', '_f', '_CI_METHOD', '_FUND_KEYS', '_is_footnote', '_clean_party', '_clean_date', '_clean_ratio', '_fmt_payment_terms', '_strip_disclaimer', '_parse_numbered_body', '_clinical_bullet', '_parse_clinical_result', '_BOND_METHOD', '_parse_etc_field', '_clean_amendment_field', '_fmt_amendment_val', '_parse_agm_notice_text', '_get_body', '_prose_diff', '_REL_MARK', '_ETC_BOILER', '_etc_segments', '_related_list', '_rel_text', '_numbered_with_lead']
