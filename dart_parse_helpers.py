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


def _fmt_payment_terms(raw: str) -> list[str]:
    """지급조건 텍스트를 줄 단위 목록으로 변환.

    입력 예: '1. 기자재비 1) 선급금: 20%~50% 2) 납품불: 45%~75% 2. 설치비 1) 착공불: 30% ...'
    출력 예:
      • 기자재비: 선급금 20~50% / 납품불 45~75% / 최종불 5%
      • 설치비: 착공불 30% / ...
    """
    # 최상위 항목 분리 (1. 2. 3. …)
    top_parts = re.split(r'(?<!\d)(\d{1,2})\.\s+', raw.strip())
    sections = []
    i = 1
    while i < len(top_parts) - 1:
        title   = top_parts[i + 1].strip()
        sections.append(title)
        i += 2

    if not sections:
        # '-' 구분 목록 처리 (예: '30% 지급 - 30% 지급 - 잔금 ...')
        dash_items = [s.strip() for s in re.split(r'\s+-\s+', raw.strip()) if s.strip()]
        if len(dash_items) > 1:
            return [f'  • {_trunc(item, 60)}' for item in dash_items[:6]]
        # 번호 목록도 dash도 없으면 truncate
        cleaned = re.sub(r'\s+', ' ', raw)
        return [f'  {_trunc(cleaned, 80)}']

    result = []
    for sec in sections[:5]:
        # 하위 항목 분리 (1) 2) 3) …)
        sub_parts = re.split(r'(?<!\d)(\d{1,2})\)\s+', sec)
        title_part = sub_parts[0].strip().rstrip(':：').strip()

        subs = []
        j = 1
        while j < len(sub_parts) - 1:
            content = re.sub(r'\s+', ' ', sub_parts[j + 1]).strip()
            # 'key: value' 분리
            m = re.match(r'^(.{1,15}?):\s*(.+)', content)
            if m:
                subs.append(f'{m.group(1).strip()} {m.group(2).strip()}')
            else:
                subs.append(_trunc(content, 30))
            j += 2
            if len(subs) >= 4:
                break

        if subs:
            result.append(f'  • {title_part}: {" / ".join(subs)}')
        elif title_part:
            result.append(f'  • {_trunc(title_part, 60)}')

    return result


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


def _parse_numbered_body(text: str, max_items: int = 8, val_limit: int = 300) -> list[str]:
    """'1) 항목명: 내용' / '1. 항목명 - 내용' 형태 번호 목록을 줄별 bullet로 변환.

    - 값 선두의 대시 불릿('- ')은 노이즈라 제거.
    - 값 안에 ' - ' 하위항목이 여럿이면(예: 신청일/승인일/조기종료일/승인기관)
      **버리지 않고** 개행+들여쓰기로 모두 표시(핵심 날짜·기관 보존).
    - 서술형(사유·향후계획 등)은 val_limit까지 넉넉히 표시(핵심 정보라 절단 최소화).
    """
    # 번호 목록 분리: '1)' / '1. ' / '1.임상'(공백없음) 모두 지원.
    # (?!\d): '0.56'·날짜('06.30')·소수는 분리 안 함. (?<!\w): '제3상'·'GV1001' 보호.
    parts = re.split(r'\s*(?<!\w)(\d{1,2})[.)](?!\d)\s*', text)
    # parts = ['prefix', '1', 'content1 ', '2', 'content2 ', ...]
    items = []
    i = 1
    while i < len(parts) - 1:
        content = parts[i + 1].strip()
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
            # 단순 섹션 헤더(짧고 콜론/값 없는 것)는 생략
            if 10 <= len(short) <= val_limit:
                items.append(f'  • {short}')
        i += 2
        if len(items) >= max_items:
            break
    return items


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


__all__ = ['log', '_get', '_trunc', '_trunc_clean', '_fetch_dart_majorstock', '_fetch_dart_reporter', '_fmt_amount', '_f', '_CI_METHOD', '_FUND_KEYS', '_is_footnote', '_clean_party', '_clean_date', '_clean_ratio', '_fmt_payment_terms', '_strip_disclaimer', '_parse_numbered_body', '_clinical_bullet', '_parse_clinical_result', '_BOND_METHOD', '_parse_etc_field', '_clean_amendment_field', '_fmt_amendment_val', '_parse_agm_notice_text']
