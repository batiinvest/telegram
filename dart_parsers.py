"""
dart_parsers.py — 공시 카테고리별 파서 + _PARSER_MAP 등록제

새 유형 추가: parse_xxx(kv) 작성 후 _PARSER_MAP에 (제목 키워드 목록, 함수) 등록.
공용 유틸(_get·_trunc 등)·원문 취득은 dart_doc, 배선은 dart_parser(파사드).
kv에는 _build_kv 결과 + '_html'(원문)·'_rcept_no'(접수번호) 내부 키가 담긴다.
"""
import re
import logging
from bs4 import BeautifulSoup

from dart_doc import (
    _get, _trunc, _trunc_clean,
    _fetch_dart_majorstock, _fetch_dart_reporter,
)

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  카테고리별 파서
#  ENG 키(ASCII, 인코딩 무관) + AUNITVALUE/TE 값으로 추출
# ══════════════════════════════════════════════

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

# 자금용도별 ENG 키 목록 (유상증자 조달금액)
_FUND_KEYS = [
    'Facility investment', 'Operating capital (KRW)',
    'Acquiring other companies', 'Debt repayment',
    'Operating capital', 'Other',
]


def parse_rights_offering(kv: dict) -> list:
    """유상증자결정 — ENG 키 기반 핵심 필드 추출"""
    lines = []

    _f(lines, kv, '🔢 신주식수', '1. Class and number of new shares', suffix='주')

    # 발행가액 + 할인율 (두 필드 조합 — 직접 처리)
    price    = _get(kv, '6. Issuing price of new shares', 'Issuing price')
    discount = _get(kv, '7-2. Discount or premium ratio', 'Discount or premium ratio (%)')
    if price:
        disc_str = f' (할인율 {discount}%)' if discount else ''
        lines.append(f'💵 발행가액: {price}원{disc_str}')

    _f(lines, kv, '📊 기준주가', '7. Base stock price', 'Base stock price: Lower', suffix='원')

    # 조달금액 (목적별 키 순회)
    for fk in _FUND_KEYS:
        if v := _get(kv, fk):
            lines.append(f'💰 조달금액: {_fmt_amount(v)}원')
            break

    _f(lines, kv, '📅 납입일', 'Payment date')
    _f(lines, kv, '📅 상장예정', 'Scheduled listing date')
    _f(lines, kv, '📋 방식', '5. Capital increase method', fmt=lambda v: _CI_METHOD.get(v, v))
    _f(lines, kv, '📋 결의일', 'Board resolution date')

    # 제3자 배정대상자 (PART/ALL_CNT 행에서 수집)
    allottees = []
    i = 0
    while f'_allottee_{i}' in kv:
        name = kv[f'_allottee_{i}']
        cnt  = kv.get(f'_allot_cnt_{i}', '')
        allottees.append((name, cnt))
        i += 1
    if allottees:
        formatted = []
        for name, cnt in allottees:
            # 인코딩 깨짐 감지 → 이름 뒤에 경고 표시
            suffix = ' ⚠️' if '?' in name else ''
            formatted.append(f'{name}{suffix} ({cnt}주)' if cnt else f'{name}{suffix}')
        if len(formatted) == 1:
            lines.append(f'🏢 배정대상: {formatted[0]}')
        else:
            lines.append('🏢 배정대상:\n' + '\n'.join(f'  • {a}' for a in formatted))

    return lines


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


def parse_contract(kv: dict) -> list:
    """단일판매ㆍ공급계약체결 / 수주"""
    lines = []

    # 계약명
    if v := _get(kv, '체결계약명', '계약명'):
        lines.append(f'📋 계약명: {_trunc(v, 60)}')

    # 계약상대 + 지역 — 각주(1. 적용환율... 형태) 필터링
    party  = _get(kv, '계약상대', '거래상대방', '발주처', '매수인')
    region = _get(kv, '판매ㆍ공급지역', '공급지역', '수주지역', '납품지역')
    if party and not _is_footnote(party):
        party_clean = _clean_party(party)
        region_str = f' ({_trunc(region, 30)})' if region and not _is_footnote(region) else ''
        lines.append(f'🏢 상대방: {party_clean}{region_str}')

    # 계약금액 + 매출비중 — '정정전/후' 복합값(금액 비율)에서 각각 분리
    amount = _get(kv, '계약금액(원)', '계약금액', '공급금액', '수주금액', '거래금액')
    ratio  = _get(kv, '매출액대비(%)', '최근매출액대비', '매출액 대비')
    if amount:
        # 복합값 "523,270,000,000 6.02" → 앞부분(금액)만 추출
        m_amt = re.search(r'계약금액\s*[：:]\s*([\d,]+)', amount)
        if m_amt:
            amt_clean = m_amt.group(1)
        else:
            # 선행 숫자(콤마 포함) 추출 — 뒤의 소수 비율 제거
            m_num = re.match(r'^([\d,]+)', amount.replace(' ', ''))
            amt_clean = m_num.group(1) if m_num else amount
        # 비율: 독립 키 우선, 없으면 복합값 끝부분에서 추출
        # 복합값 "523,270,000,000 6.02" 대응: 큰 숫자 포함 시 끝 소수만 추출
        if ratio and re.search(r'[\d,]{6,}', ratio):
            m_r = re.search(r'\s(\d{1,3}(?:\.\d+)?)$', ratio.strip())
            ratio = m_r.group(1) if m_r else None
        if ratio:
            ratio_clean = _clean_ratio(ratio)
        else:
            m_ratio = re.search(r'\s+([\d.]+)$', amount.strip())
            ratio_clean = (m_ratio.group(1) + '%') if m_ratio else ''
        ratio_str = f' (매출대비 {ratio_clean.rstrip("%")}%)' if ratio_clean else ''
        lines.append(f'💰 계약금액: {_fmt_amount(amt_clean)}원{ratio_str}')

    # 계약기간 — 각주 필터링, 날짜만 추출
    # 기재정정 복합값 "2025-04-15 2029-04-30" 대응: 날짜 두 개 모두 추출
    start = _get(kv, '시작일')
    end   = _get(kv, '종료일')

    def _extract_dates(v: str) -> list[str]:
        """문자열에서 YYYY-MM-DD 형식 날짜 모두 추출."""
        return re.findall(r'\d{4}-\d{2}-\d{2}', v) if v else []

    # 시작일 키에 두 날짜가 함께 있는 경우(복합값) 분리
    start_dates = _extract_dates(start) if start and not _is_footnote(start) else []
    end_dates   = _extract_dates(end)   if end   and not _is_footnote(end)   else []

    if len(start_dates) >= 2 and (not end_dates or start_dates == end_dates):
        # "2025-04-15 2029-04-30" 복합값 → 시작/종료 분리
        start_clean, end_clean = start_dates[0], start_dates[1]
    else:
        start_clean = start_dates[0] if start_dates else None
        # 종료일 복합값에서는 마지막 날짜 사용
        end_clean   = end_dates[-1]  if end_dates   else None

    # 정정 섹션에서 종료일만 바뀐 경우 KV 직접 탐색
    if not end_clean:
        for k, v in kv.items():
            if '종료일' in k and v and '정정전' not in k:
                dates = _extract_dates(v)
                if dates:
                    end_clean = dates[-1]  # 복합값이면 마지막 날짜(종료일)
                    break
    if start_clean and end_clean:
        lines.append(f'📅 계약기간: {start_clean} ~ {end_clean}')
    elif end_clean:
        lines.append(f'📅 종료일: {end_clean}')
    elif start_clean:
        lines.append(f'📅 시작일: {start_clean}')

    # 지급조건 — 중첩 번호 목록을 줄 단위로 정리
    if v := _get(kv, '대금지급 조건', '지급조건', '대금지급'):
        pay_lines = _fmt_payment_terms(v)
        if pay_lines:
            lines.append('💳 지급조건:')
            lines.extend(pay_lines)

    return lines


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


def _parse_clinical_result(text: str, max_sections: int = 5, sec_limit: int = 600) -> list:
    """임상시험결과 '결과값'을 '- 섹션명:' 단위로 분리해 bullet로 반환.

    예) '- 항바이러스 활성: ... - 안전성, 내약성: ... - 약동학: ...'
    → 섹션별 라인. 섹션 헤더가 없으면 빈 리스트(호출측에서 단순 truncate fallback).
    본문 내 인라인 콜론('200 mg:', 'Dose:')은 앞에 ' - '가 없어 오분리되지 않음.
    결과는 핵심 정보라 섹션당 넉넉히(600자) 표시하고, 용량행('N mg:')·'위약:'은
    개행+들여쓰기해 용량반응·약동학 표를 세로로 정렬(가독성).
    """
    text = re.sub(r'\s+', ' ', text).strip()
    parts = re.split(r'(?:^|\s)-\s+([가-힣][가-힣,·\s]{0,14}):\s+', text)
    if len(parts) < 3:   # 섹션 헤더 못 찾음
        return []
    lines = []
    for i in range(1, len(parts), 2):
        label = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ''
        if not content:
            continue
        content = _trunc_clean(content, sec_limit)
        # 용량행·위약 앞 개행 → 표 형태로 세로 정렬
        content = re.sub(r'\s+(?=(?:\d[\d,]*\s*mg|위약)\s*:)', '\n      ', content)
        lines.append(f'  • {label}: {content}')
        if len(lines) >= max_sections:
            break
    return lines


def parse_mgmt_event(kv: dict) -> list:
    """투자판단관련주요경영사항 — 임상·기술이전·계약 등"""
    lines = []

    # 자회사 여부
    subsidiary = _get(kv, '자회사인')
    if subsidiary:
        lines.append(f'🏢 자회사: {subsidiary}')

    # 제목
    if v := _get(kv, '1. 제목', '제목'):
        lines.append(f'📌 {_trunc(v, 80)}')

    # 주요내용 파싱 — 두 가지 구조 처리
    body_raw = _get(kv, '2. 주요내용', '주요내용', '결정내용') or ''
    stripped = _strip_disclaimer(body_raw).strip()

    # [구조 A] 번호 항목이 KV 개별 행으로 분리된 경우
    # '2. 주요내용' 값이 짧거나 '1) ...' 레이블만 있는 경우
    numbered_kv = {
        k: v for k, v in kv.items()
        if re.match(r'^\d{1,2}\)\s+\S', k)
    }
    if numbered_kv and len(stripped) < 30:
        count = 0
        for k, v in numbered_kv.items():
            v = re.sub(r'\s+', ' ', v).strip()
            if not v or len(v) > 60:
                continue
            label = re.sub(r'^\d+\)\s*', '', k).strip()
            lines.append(f'  • {label}: {v}')
            count += 1
            if count >= 6:
                break
    elif stripped and len(stripped) > 5:
        # [구조 B] 하나의 긴 문자열 안에 번호 목록 포함
        bullets = _parse_numbered_body(stripped)
        if bullets:
            lines.extend(bullets)
        else:
            # 앞의 '- ' 또는 '· ' 제거 후 산문 전문 표시 (극단 케이스만 공백경계 절단)
            clean = re.sub(r'^[\-·•]\s*', '', re.sub(r'\s+', ' ', stripped)).strip()
            lines.append(f'  {_trunc_clean(clean, 800)}')

    # 시험결과 (임상시험결과 공시) — '- 섹션:' 구조면 섹션별 분리, 아니면 단순 표시
    result_val = _get(kv, '2) 결과값', '결과값')
    if result_val:
        sec_lines = _parse_clinical_result(result_val)
        if sec_lines:
            lines.append('🔬 결과:')
            lines.extend(sec_lines)
        else:
            lines.append(f'🔬 결과: {_trunc_clean(result_val, 500)}')

    # 변경신청 사유 (변경승인 공시)
    if v := _get(kv, '3. 변경신청 사유', '변경신청 사유', '변경사유'):
        bullets = _parse_numbered_body(v, max_items=5)
        if bullets:
            lines.append('📋 변경사유:')
            lines.extend(bullets)
        else:
            lines.append(f'📋 변경사유: {_trunc(v, 100)}')

    # 결정일 / 사실확인일
    if v := _get(kv, '4. 사실발생', '이사회결의일', '사실확인일', '결정일'):
        lines.append(f'📅 결정일: {v}')

    # 관련공시
    if v := _get(kv, '관련공시', '※ 관련 공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

    return lines


def parse_combined_ci(kv: dict) -> list:
    """유무상증자결정 — 유상증자 + 무상증자 합산 파서.
    섹션 번호(ENG 키의 앞 숫자)로 유상/무상 구분:
      유상증자: 12. Payment date, 16. Scheduled listing ...
      무상증자: 5. Number per share (배정비율), 4. Record date (기준일), 8. Scheduled listing ...
    """
    lines = []

    # ── 유상증자 ───────────────────────────────────────
    paid_count = _get(kv, '1. Class and number of new shares')
    if paid_count:
        lines.append('【유상증자】')
        lines.append(f'🔢 신주식수: {paid_count}주')

    # 조달금액 전체 합산 (중복 방지: 이미 집계한 값 skip)
    _SUM_FUND_KEYS = [
        'Facility investment',
        'Operating capital (KRW)',
        'Debt repayment (KRW)',
        'Acquiring other companies (KRW)',
        'Other purpose',
    ]
    total_fund = 0
    seen_fund_vals: set = set()
    for fk in _SUM_FUND_KEYS:
        if v := _get(kv, fk):
            if v not in seen_fund_vals:
                try:
                    total_fund += int(v.replace(',', ''))
                    seen_fund_vals.add(v)
                except (ValueError, AttributeError):
                    pass
    if total_fund:
        lines.append(f'💰 조달금액: {_fmt_amount(str(total_fund))}원')

    if v := _get(kv, '5. Capital increase method'):
        lines.append(f'📋 방식: {_CI_METHOD.get(v, v)}')

    if v := _get(kv, '12. Payment date'):
        lines.append(f'📅 납입일: {v}')

    if v := _get(kv, '16. Scheduled listing date'):
        lines.append(f'📅 상장예정: {v}')

    # ── 무상증자 ───────────────────────────────────────
    bonus_ratio   = _get(kv, '5. Number of new stocks allocated per share')
    bonus_record  = _get(kv, '4. Record date for allotment')
    bonus_listing = _get(kv, '8. Scheduled listing date of new shares')

    if bonus_ratio:
        lines.append('【무상증자】')
        try:
            ratio = float(bonus_ratio)
            pct = int(ratio * 100)
            lines.append(f'📊 1주당 {bonus_ratio}주 배정 (100주→{pct}주)')
        except (ValueError, TypeError):
            lines.append(f'📊 1주당 배정: {bonus_ratio}주')
        if bonus_record:
            lines.append(f'📅 기준일: {bonus_record}')
        if bonus_listing:
            lines.append(f'📅 상장예정: {bonus_listing}')

    return lines


_BOND_METHOD = {'1': '공모', '2': '사모', '3': '주주배정', '4': '기타'}


def parse_cb(kv: dict) -> list:
    """전환사채(CB) / 신주인수권부사채(BW) 파서"""
    lines = []

    _f(lines, kv, '💰 발행금액', '2. Total face', 'Total face (or electronically registered) value', fmt=_fmt_amount, suffix='원')
    _f(lines, kv, '💵 전환가액', 'Conversion price (KRW/share)', 'Exercise price', suffix='원/주')

    # 이자율 / 만기수익률 (두 필드 조합)
    coupon = _get(kv, 'Coupon rate', '4. Interest rate of bonds')
    ytm    = _get(kv, 'Yield to maturity')
    if coupon:
        ytm_str = f' / YTM {ytm}%' if ytm and ytm != coupon else ''
        lines.append(f'📊 이자율: {coupon}%{ytm_str}')

    _f(lines, kv, '📅 만기', '5. Bond maturity date', 'Maturity date')

    # 전환청구기간 (두 필드 조합)
    start = _get(kv, 'Start date')
    end   = _get(kv, 'End date')
    if start and end:
        lines.append(f'📅 전환청구: {start} ~ {end}')

    _f(lines, kv, '📋 발행방식', '8. Method of bond issuance', fmt=lambda v: _BOND_METHOD.get(v, v))
    _f(lines, kv, '📅 납입일', '12. Payment date', 'Payment date')

    return lines


def parse_ex_rights(kv: dict) -> list:
    """권리락 — 기준가·실시일·사유 추출.
    KV 구조: 6열 테이블이 (헤더1:헤더2, 코드:기준가, 날짜:사유) 쌍으로 저장됨."""
    lines = []

    # 기준가: 'A숫자' 형식 키(종목코드) → 기준가 값
    for k, v in kv.items():
        if re.match(r'^A\d+$', k) and v:
            lines.append(f'💹 기준가: {v}원')
            break

    # 권리락 실시일 + 사유: 날짜 형식 키 → 사유 값
    for k, v in kv.items():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', k):
            lines.append(f'📅 실시일: {k}')
            if v:
                lines.append(f'📋 사유: {v}')
            break

    return lines


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


def parse_trading_halt(kv: dict) -> list:
    """주권매매거래정지 / 기간변경 / 해제"""
    lines = []

    # ── 거래정지 해제 형식 ───────────────────────────────────────
    release = _get(kv, '2.해제사유', '해제사유')
    if release:
        lines.append(f'🔓 {release}')
        if v := _get(kv, '1.대상종목', '대상종목'):
            lines.append(f'📋 대상: {v}')
        halt_dt = _get(kv, '3.해제일시', '해제일시') or ''
        if halt_dt:
            lines.append(f'📅 해제일시: {halt_dt.rstrip(" -").strip()}')
        if v := _get(kv, '5.기타', '기타'):
            lines.extend(_parse_etc_field(v))
        if v := _get(kv, '4.근거규정', '근거규정'):
            lines.append(f'📋 근거: {_trunc(v, 60)}')
        return lines

    # ── 기간변경 형식 ────────────────────────────────────────────
    reason = _get(kv, '2.변경사유', '변경사유')
    before = _get(kv, '가.변경전', '변경전')
    after  = _get(kv, '나.변경후', '변경후')

    if reason or before or after:
        if reason:
            lines.append(f'🚨 {reason}')
        if v := _get(kv, '1.대상종목', '대상종목'):
            lines.append(f'📋 대상: {v}')
        if before:
            lines.append(f'  변경전: {_trunc(before, 80)}')
        if after:
            lines.append(f'  변경후: {_trunc(after, 80)}')
        if v := _get(kv, '4.근거규정', '근거규정'):
            lines.append(f'📋 근거: {_trunc(v, 60)}')
        return lines

    # ── 일반 거래정지 형식 ───────────────────────────────────────
    if v := _get(kv, '2.정지사유', '정지사유'):
        lines.append(f'⏸️ 정지사유: {v}')

    # 정지일시: 날짜 형식 키 탐색, 값 끝 ' -' 제거
    for k, v in kv.items():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', k):
            time_part = v.rstrip(' -').strip() if v else ''
            dt_str = f'{k} {time_part}'.strip() if time_part else k
            lines.append(f'🕐 정지일시: {dt_str}')
            break

    # 해제조건/만료일시 — 날짜면 '재개일시', 문장이면 '해제조건'
    if v := _get(kv, '나.만료일시', '만료일시', '재개일시'):
        v_clean = v.strip()
        label = '📅 재개일시' if re.match(r'^\d{4}-\d{2}-\d{2}', v_clean) else '📋 해제조건'
        lines.append(f'{label}: {_trunc(v_clean, 100)}')

    return lines


def parse_debt_guarantee(kv: dict) -> list:
    """타인에 대한 채무보증결정"""
    lines = []

    # 채무자 + 관계
    debtor   = _get(kv, '1. 채무자', '채무자')
    relation = _get(kv, '-회사와의 관계', '회사와의 관계')
    if debtor:
        rel_str = f' ({relation})' if relation else ''
        lines.append(f'🏢 채무자: {debtor}{rel_str}')

    _f(lines, kv, '🏦 채권자', '2. 채권자', '채권자')
    _f(lines, kv, '💳 차입금액', '3. 채무(차입)금액(원)', '채무(차입)금액', fmt=_fmt_amount, suffix='원')

    # 보증금액 + 자기자본 대비
    guarantee = _get(kv, '채무보증금액(원)', '보증금액')
    ratio     = _get(kv, '자기자본대비(%)')
    if guarantee:
        ratio_str = f' (자기자본 대비 {ratio}%)' if ratio else ''
        lines.append(f'💰 보증금액: {_fmt_amount(guarantee)}원{ratio_str}')

    # 보증기간
    start = _get(kv, '시작일')
    end   = _get(kv, '종료일')
    start_clean = _clean_date(start) if start else None
    end_clean   = _clean_date(end)   if end   else None
    if start_clean and end_clean:
        lines.append(f'📅 보증기간: {start_clean} ~ {end_clean}')

    _f(lines, kv, '📋 결의일', '6. 이사회결의일(결정일)', '이사회결의일')

    # 기타 참고사항 (첫 문장만)
    if v := _get(kv, '7. 기타 투자판단에 참고할 사항', '기타 투자판단'):
        note = re.sub(r'^\([\d]+\)\s*', '', v).strip()
        first = re.split(r'[.。]\s*\(', note)[0].strip()
        if first:
            lines.append(f'  {_trunc(first, 70)}')

    return lines


def parse_trust_termination_decision(kv: dict) -> list:
    """자기주식 신탁계약 해지결정"""
    lines = []

    _f(lines, kv, '💰 계약금액', '1. Contract amount (KRW)', 'Contract amount', fmt=_fmt_amount, suffix='원')

    start = _get(kv, '2. Contract period before termination')
    end   = _get(kv, 'End date')
    if start and end:
        lines.append(f'📅 계약기간: {start} ~ {end}')

    if v := _get(kv, '3. Purpose of termination', 'Purpose of termination'):
        # 대체문자를 제거하고 읽을 수 있는 내용만 표시
        cleaned = re.sub(r'[?�]+', '', v).strip()
        cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
        if cleaned and len(cleaned) > 4:
            lines.append(f'📋 해지사유: {_trunc(cleaned, 50)}')

    if v := _get(kv, '4. Termination institution', 'Termination institution'):
        v = re.sub(r'\s*\(.*\)\s*$', '', v).strip()
        lines.append(f'🏦 해지기관: {v}')

    _f(lines, kv, '📅 해지예정일', '5. Scheduled termination date', 'Scheduled termination date')

    return lines


def parse_trust_termination(kv: dict) -> list:
    """자기주식 신탁계약 해지결과보고서"""
    lines = []

    # 수탁사
    trust_co = _get(kv, '회사명 :', '회사?') or ''
    for k, v in kv.items():
        if '회사' in k and '자' not in k and v and 'NH' in v or ('증권' in v and len(v) < 50):
            trust_co = re.sub(r'\(.*\)', '', v).strip()
            break
    if trust_co:
        lines.append(f'🏦 수탁사: {_trunc(trust_co, 30)}')

    # 해지일: '자기주식 취득을 위한...' 키 → 해지일 값
    term_date = ''
    for k, v in kv.items():
        if '취득을 위' in k and re.match(r'\d{4}-\d{2}-\d{2}', v or ''):
            term_date = v
            break
    if term_date:
        lines.append(f'📅 해지일: {term_date}')

    # 취득 결과: 가장 큰 숫자(금액) → 취득금액, 수량
    amounts = []
    for k, v in kv.items():
        if re.match(r'^[\d,]+$', k):
            try:
                n = int(k.replace(',', ''))
                if n > 1_000_000:  # 100만 이상 = 금액
                    amounts.append(n)
            except ValueError:
                pass
    if amounts:
        total = max(amounts)
        lines.append(f'💰 취득금액: {_fmt_amount(str(total))}원')

    # 취득수량: 쉼표 포함 포맷된 숫자(e.g. 332,905)를 우선 탐색
    for k, v in kv.items():
        if v and re.match(r'^\d{1,3}(,\d{3})+$', v):  # 쉼표 포함 천단위 형식
            try:
                n = int(v.replace(',', ''))
                if 1_000 < n < 10_000_000:
                    lines.append(f'🔢 취득수량: {n:,}주')
                    break
            except ValueError:
                pass

    return lines


def parse_treasury_acquisition(kv: dict) -> list:
    """자기주식 취득 신탁계약 체결 / 직접취득 결정"""
    lines = []

    _f(lines, kv, '💰 취득금액', '1. Contract amount (KRW)', 'Contract amount', fmt=_fmt_amount, suffix='원')

    # 취득예정 주식수 + 단가
    shares = _get(kv, '9. Number of shares to be acquired', 'Number of shares to be acquired')
    price  = _get(kv, '10. Price of shares to be acquired', 'Price of shares to be acquired')
    if shares:
        price_str = f' (주당 {price}원)' if price else ''
        lines.append(f'🔢 취득예정: {shares}주{price_str}')

    # 계약기간
    start = _get(kv, 'Start date', '2. Contract period')
    end   = _get(kv, 'End date')
    if start and end and start != end:
        lines.append(f'📅 계약기간: {start} ~ {end}')
    elif start:
        lines.append(f'📅 계약일: {start}')

    # 목적 (인코딩 깨진 경우 생략)
    if v := _get(kv, '3. Purpose of contract', 'Purpose'):
        if '?' not in v and '�' not in v:
            lines.append(f'📋 목적: {_trunc(v, 40)}')

    # 수탁사
    if v := _get(kv, '4. Counterparty (Trust company)', 'Counterparty'):
        # 영문 괄호 이후 제거
        v = re.sub(r'\s*\(.*\)\s*$', '', v).strip()
        lines.append(f'🏦 수탁사: {v}')

    _f(lines, kv, '📋 결의일', '7. Board resolution date', 'Board resolution date')

    return lines


def parse_preliminary_earnings(kv: dict) -> list:
    """연결/별도 잠정실적 공정공시"""
    lines = []

    # 보고 기간 추출: 당기실적 키의 값이 시작일
    period_start = _get(kv, '당기실적')
    # 분기 레이블: 두 가지 패턴 지원
    #   ('26.1Q)  →  26.1Q
    #   (2026년 1분기)  →  26.1Q 변환
    quarter_label = ''
    for k in kv:
        m = re.match(r"^\('?([0-9]{2})\.([0-9])Q?\)", k)
        if m:
            quarter_label = f"{m.group(1)}.{m.group(2)}Q"
            break
        m2 = re.match(r'^\(([0-9]{4})년\s*([0-9])분기\)', k)
        if m2:
            quarter_label = f"{m2.group(1)[2:]}.{m2.group(2)}Q"
            break

    # 연결/별도 구분 (공시 제목 또는 1번 항목 키로 판단)
    report_type = '연결' if any('연결' in k for k in list(kv.keys())[:15]) else '별도'

    header = f'📊 {report_type} 잠정실적'
    if quarter_label:
        header += f" ('{quarter_label})"
    lines.append(header)

    # 실적 지표 추출: items 순서 기반
    # 구조: (지표명, '당해실적'), (당기값, 전기값), (QoQ증감율, '-'), (전년동기값, YoY증감율)
    items = list(kv.items())
    _METRICS = {'매출액', '영업이익', '당기순이익', '지배기업 소유주지분 순이익',
                '법인세비용차감전계속사업이익'}
    _IS_NUM = re.compile(r'^-?[\d,]+(\.\d+)?$')
    _IS_PCT = re.compile(r'^-?[\d.]+$')

    def _fmt_amt(v: str) -> str:
        try:
            n = int(v.replace(',', ''))
            return _fmt_amount(str(abs(n) * 1_000_000))  # 단위: 백만원
        except (ValueError, AttributeError):
            return v

    def _pct_str(v: str) -> str:
        if not v or v in ('-', ''):
            return ''
        if '흑자' in v or '적자' in v:
            return v
        if _IS_PCT.match(v):
            try:
                f = float(v)
            except ValueError:   # '1.2.3' 등 _IS_PCT 오매치
                return ''
            # 표 구조 어긋남 방어: 증감률 자리에 금액이 잘못 매칭되면
            # 소수점 없는 큰 정수로 나타남 → 표기 생략(금액만 표시)
            if abs(f) >= 10000 or ('.' not in v and abs(f) >= 1000):
                return ''
            sign = '+' if f > 0 else ''
            return f'{sign}{v}%'
        return ''

    for idx, (k, v) in enumerate(items):
        if k not in _METRICS:
            continue
        # 다음 4개 items에서 값 추출
        if idx + 3 >= len(items):
            continue

        # (당기값, 전기값) 쌍
        k1, curr = items[idx + 1]
        k2, qoq  = items[idx + 2]   # QoQ 증감율이 key
        k3, yoy  = items[idx + 3]   # 전년동기값이 key (YoY 증감율이 value)

        if not _IS_NUM.match(k1):
            continue

        amt_str = _fmt_amt(k1)
        qoq_str = _pct_str(k2)
        yoy_str = _pct_str(yoy) if _IS_NUM.match(k3) else ''

        parts = [amt_str]
        if qoq_str:
            parts.append(f'QoQ {qoq_str}')
        if yoy_str:
            parts.append(f'YoY {yoy_str}')

        label = '매출' if k == '매출액' else ('영업이익' if k == '영업이익' else
                 '순이익' if k == '당기순이익' else
                 '지배순이익' if '지배기업' in k else '세전이익')
        lines.append(f'  {label}: {" / ".join(parts)}')

    return lines


def parse_value_enhancement(kv: dict) -> list:
    """기업가치제고계획 (자율공시)"""
    lines = []

    # 계획서 명칭
    if v := _get(kv, '1. 계획서 명칭', '계획서 명칭'):
        lines.append(f'📋 {_trunc(v, 50)}')

    # 주요내용 — 섹션별 전체 표시
    body = _get(kv, '2. 주요 내용', '주요 내용') or ''
    if body:
        # '<섹션명> -. bullet1 -. bullet2 ...' 구조 파싱
        # 섹션 단위로 분리
        parts = re.split(r'<([^>]+)>', body)
        # parts = ['prefix', '섹션1', '불릿들...', '섹션2', '불릿들...', ...]
        i = 1
        while i < len(parts) - 1:
            section_name = parts[i].strip()
            content = parts[i + 1]
            bullets = [b.strip() for b in re.findall(r'(?<!\w)-\.\s*(.+?)(?=\s+-\.|<|$)', content) if b.strip()]
            if section_name or bullets:
                if section_name:
                    lines.append(f'【{section_name}】')
                for b in bullets:
                    lines.append(f'  • {_trunc(b, 60)}')
            i += 2

    # 고배당기업 여부
    if v := _get(kv, '3. 조세특례제한법', '고배당기업'):
        if '해당' in v:
            lines.append('📌 고배당기업 해당')

    # 배당성향 + 배당금액 + 증가율
    ratio    = _get(kv, '직전 사업연도', '배당성향(%)')
    div_amt  = _get(kv, '직전 사업연도 (2025) 이익배당금액(원)', '이익배당금액(원)')
    growth   = _get(kv, '전전 사업연도 대비 직전 사업연도 이익배당금액 증가율(%)', '증가율(%)')

    # 배당성향 키가 연도 포함이라 _get substring 매칭 이용
    for k, v in kv.items():
        if '배당성향' in k and v and re.match(r'[\d.]+', v):
            ratio = v
            break

    if ratio or div_amt:
        parts = []
        if ratio:
            parts.append(f'배당성향 {ratio}%')
        if div_amt:
            parts.append(f'배당금 {_fmt_amount(div_amt)}원')
        if growth:
            parts.append(f'YoY +{growth}%')
        lines.append(f'💰 {" / ".join(parts)}')

    _f(lines, kv, '📅 결정일', '4. 결정일자', '결정일자')
    _f(lines, kv, '🔗 관련', '※ 관련공시', '관련공시', trunc=50)

    return lines


def parse_outside_director(kv: dict) -> list:
    """사외이사 선임·해임·중도퇴임 신고"""
    lines = []

    appoint  = _get(kv, 'Appointment/reappointment (persons)', 'Appointment')
    dismiss  = _get(kv, 'Dismissal/resignation (persons)', 'Dismissal')
    total_chg = _get(kv, '2. Number of outside directors changed')

    parts = []
    if appoint and appoint not in ('0', '-'):
        parts.append(f'선임 {appoint}명')
    if dismiss and dismiss not in ('0', '-'):
        parts.append(f'해임/퇴임 {dismiss}명')
    if not parts and total_chg:
        parts.append(f'변경 {total_chg}명')

    if parts:
        lines.append(f'👔 사외이사 {" / ".join(parts)}')

    # 변경 전 현황
    before_total   = _get(kv, '3. Status before change of outside directors')
    after_total    = _get(kv, '4. Status after change of outside director')
    after_outside  = _get(kv, 'Total number of outside directors (persons)')
    after_reg      = _get(kv, 'Total number of registered directors (persons)')
    ratio          = _get(kv, 'Outside director ratio (%)')

    # 변경 전 사외이사 수 역산 (선임 - 해임 기준)
    try:
        ap = int(appoint or 0)
        di = int(dismiss or 0)
        ao = int(after_outside or 0)
        before_outside = ao - ap + di
        if before_total:
            lines.append(f'  변경전: 등기이사 {before_total}명 (사외이사 {before_outside}명)')
        if after_reg and after_outside and ratio:
            lines.append(f'  변경후: 등기이사 {after_reg}명 (사외이사 {after_outside}명, {ratio}%)')
    except (ValueError, TypeError):
        if after_outside and ratio:
            lines.append(f'📊 사외이사: {after_outside}명/{after_reg}명 ({ratio}%)')

    _f(lines, kv, '📅 변경일', '1. Date of change outside director', '1. Date of change in outside director')

    return lines


def parse_hq_relocation(kv: dict) -> list:
    """본점소재지변경
    _build_kv는 '주소' 키 중복 시 마지막 값만 보존하므로
    kv['_html']에서 BeautifulSoup으로 변경전/후 주소를 직접 파싱.
    """
    lines = []
    before = after = ''

    html = kv.get('_html', '')
    if html:
        import warnings
        try:
            from bs4 import XMLParsedAsHTMLWarning
            warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)
        except ImportError:
            pass
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        section = ''
        for row in soup.find_all('tr'):
            cells = [re.sub(r'\s+', ' ', c.get_text(' ', strip=True)) for c in row.find_all(['td', 'th'])]
            cells = [c for c in cells if c]
            if not cells:
                continue
            if '가. 변경전' in cells[0]:
                section = 'before'
            elif '나. 변경후' in cells[0]:
                section = 'after'
            elif cells[0] == '주소' and len(cells) >= 2:
                if section == 'before' and not before:
                    before = cells[1]
                elif section == 'after' and not after:
                    after = cells[1]

    lines.append('📍 본점 이전')
    if before:
        lines.append(f'  변경전: {_trunc(before, 55)}')
    if after:
        lines.append(f'  변경후: {_trunc(after, 55)}')

    _f(lines, kv, '📋 사유', '2. 변경사유', '변경사유', trunc=60)
    _f(lines, kv, '📅 이전일', '3. 이전(예정)일', '이전(예정)일', '이전일')

    return lines


def parse_executive_change(kv: dict) -> list:
    """대표이사 / 임원 변경"""
    lines = []

    # 변경전: '1. 변경내용' 값에서 이름 추출 ('변경전 대표이사 홍길동' 형태)
    before_raw = _get(kv, '1. 변경내용', '변경내용') or ''
    before = re.sub(r'^변경전\s*(대표이사|임원|이사|감사)?\s*', '', before_raw).strip()

    # 변경후: '변경후 대표이사' 키 → 값, 또는 다른 패턴
    after = ''
    for k, v in kv.items():
        if '변경후' in k and v and v not in ('-', ''):
            after = v
            break

    if before or after:
        lines.append('🔄 대표이사 변경')
        if before:
            lines.append(f'  변경전: {before}')
        if after:
            lines.append(f'  변경후: {after}')

    # 변경후 이사 상세 정보
    # KV 패턴: after_name(key) → 소속회사(val), 이후 직위(key) → 입사일(val)
    _SKIP_KEYS = {'상장여부', '직위', '퇴사연월일', '최대주주와의 관계', '성명', '-',
                  '상장(코스닥)', '상장(유가)', '지분비율(%)', '불참(명)', '최대주주와의 관계'}
    if after:
        items = list(kv.items())
        for idx, (k, v) in enumerate(items):
            if k == after and v and v not in ('-', ''):
                company = v
                lines.append(f'  📌 소속: {company}')
                # 최근 직위 1개 추출 (직위→입사일 패턴)
                for jk, jv in items[idx + 1:idx + 8]:
                    if jk in _SKIP_KEYS or not jv or jv in ('-',):
                        continue
                    if re.match(r'^\d{4}-\d{2}-\d{2}$', jk):
                        break  # 퇴사일 도달
                    if re.match(r'^\d{4}-\d{2}-\d{2}$', jv):
                        lines.append(f'  💼 직위: {jk} ({jv[:7]}~)')
                        break
                break

    if v := _get(kv, '2. 변경사유', '변경사유'):
        lines.append(f'📋 사유: {_trunc(v, 60)}')

    if v := _get(kv, '3. 변경일', '변경일'):
        lines.append(f'📅 변경일: {v}')

    if v := _get(kv, '※ 관련공시', '관련공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

    return lines


def parse_agm_result(kv: dict) -> list:
    """주주총회결과 (정기/임시)"""
    lines = []

    if v := _get(kv, '5. 주주총회일자', '4. 주주총회일자', '주주총회일자'):
        lines.append(f'📅 총회일: {v}')

    # 임원선임 현황
    if v := _get(kv, '가. 임원선임 현황', '임원선임 현황'):
        if v and v not in ('-', '해당없음'):
            lines.append(f'👔 임원선임: {v}')

    # 의안별 결과: 'N호의안' 키 이후 agenda→가결/부결, 그 다음에 참석%→찬성% 순
    items = list(kv.items())
    agendas = []
    for idx, (k, _) in enumerate(items):
        if not re.match(r'^\d+-?\d*호의안$', k):
            continue
        if idx + 1 >= len(items):
            break
        name, result = items[idx + 1]
        if result not in ('가결', '부결'):
            continue
        # 찬성률: 다음 KV의 value (행사주식 대비 찬성%)
        approval = ''
        if idx + 2 < len(items):
            _, ap = items[idx + 2]
            if re.match(r'^\d+\.?\d*$', ap):
                approval = f' ({ap}%)'
        icon = '✅' if result == '가결' else '❌'
        # 의안명 정리: 날짜범위·'의 건' 등 제거
        short = re.sub(r'\([^)]*\d{4}[^)]*\)', '', name)  # (2025.01.01~...) 제거
        short = re.sub(r'\s*의\s?건$', '', short).strip()
        agendas.append(f'  {icon} {_trunc(short, 25)}{approval}')

    if agendas:
        lines.append('📋 의안:')
        lines.extend(agendas[:7])

    return lines


def parse_ir_event(kv: dict) -> list:
    """기업설명회(IR) 개최"""
    lines = []

    # 일시: YYYY-MM-DD 형식 키 → 종료일 값으로 저장된 구조 처리
    start_d = end_d = start_t = end_t = ''
    for k, v in kv.items():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', k):
            start_d = k
            end_d   = v if re.match(r'^\d{4}-\d{2}-\d{2}$', v) else ''
            break
    for k, v in kv.items():
        if re.match(r'^\d{2}:\d{2}$', k):
            start_t = k
            end_t   = v if re.match(r'^\d{2}:\d{2}$', v) else ''
            break
    if start_d:
        date_str = f'{start_d} ~ {end_d}' if end_d and end_d != start_d else start_d
        time_str = f' ({start_t}~{end_t})' if start_t and end_t else ''
        lines.append(f'📅 일시: {date_str}{time_str}')

    _f(lines, kv, '📍 장소', '2. 장소', '장소', trunc=40)
    _f(lines, kv, '👥 대상', '3. 대상자', '대상자')
    _f(lines, kv, '📋 목적', '4. 실시목적', '실시목적', trunc=50)
    _f(lines, kv, '📋 내용', '6. 주요내용', '주요내용', trunc=50)

    return lines


def parse_equity_acquisition(kv: dict) -> list:
    """타법인주식 및 출자증권 취득결정"""
    lines = []

    # 종속회사 경유 여부
    sub = _get(kv, '종속회사인')
    if sub:
        lines.append(f'🏢 종속회사: {sub}')

    # 발행회사 (취득 대상)
    for k, v in kv.items():
        if k not in ('종속회사인',) and v in ('대표이사', '대표자') and k not in ('-', ''):
            lines.append(f'🏭 발행회사: {k}')
            break

    # 취득주식수 + 취득금액
    shares = _get(kv, '취득주식수(주)')
    amount = _get(kv, '취득금액(원)')
    ratio  = _get(kv, '지배회사의 연결자산총액대비(%)', '연결자산총액대비(%)')
    if shares:
        lines.append(f'🔢 취득주식수: {shares}주')
    if amount:
        ratio_str = f' (연결자산 대비 {ratio}%)' if ratio else ''
        lines.append(f'💰 취득금액: {_fmt_amount(amount)}원{ratio_str}')

    _f(lines, kv, '📊 취득 후 지분', '지분비율(%)', suffix='%')
    _f(lines, kv, '📋 취득방법', '4. 취득방법', '취득방법', trunc=60)
    _f(lines, kv, '📋 목적', '5. 취득목적', '취득목적', trunc=60)
    _f(lines, kv, '📅 취득예정', '6. 취득예정일자', '취득예정일자')
    _f(lines, kv, '🔗 관련', '※ 관련공시', '관련공시', trunc=50)

    return lines


def parse_agm_notice(kv: dict) -> list:
    """주주총회소집결의 / 소집공고.

    소집결의(이사회 결의)는 KV 테이블(날짜·장소 등)에서 추출되지만,
    소집공고는 자유서식 본문이라 KV가 비어 범용 파서가 난독 출력 → 이 경우
    본문 '아래' 섹션(일시·장소·부의안건) 텍스트 파싱으로 폴백.
    """
    lines = []

    date = _get(kv, '날짜', 'Date')
    time = _get(kv, '시간', 'Time')
    if date:
        lines.append(f'📅 일시: {date}' + (f' {time}' if time else ''))

    _f(lines, kv, '📍 장소', '2. 장소', '장소', 'Place', trunc=50)
    _f(lines, kv, '📋 구분', '-주주총회 구분', '주주총회 구분')
    _f(lines, kv, '📋 의결권기준일', '3. 의결권행사기준일', '의결권행사기준일')
    _f(lines, kv, '🔗 관련', '관련공시', '※관련공시', trunc=50)

    # KV 테이블에서 못 뽑음(소집공고 자유서식) → 본문 텍스트 파싱 폴백
    if not lines:
        return _parse_agm_notice_text(kv)

    return lines


def parse_stock_option(kv: dict) -> list:
    """주식매수선택권부여"""
    lines = []

    # 부여대상자 직위 추출: '(직 위) 상무보' 형식의 garbled 값에서 파싱
    for k, v in kv.items():
        m = re.search(r'\)\s*(\S{2,5})$', v or '')
        if m and k and re.search(r'랄\s*자|대\s*상\s*자|부\s*여', k):
            lines.append(f'👤 부여대상: {m.group(1)}')
            break

    if v := _get(kv, '1. Number of recipients', 'Number of recipients'):
        lines.append(f'👥 부여인원: {v}명')

    shares = _get(kv, '2. Number of shares granted', 'Number of shares granted')
    if shares:
        lines.append(f'🔢 부여주식수: {shares}주')

    # 행사가액: 'Exercise price (KRW)' 우선, 없으면 'Exercise price'
    price = _get(kv, 'Exercise price (KRW)', 'Exercise price')
    if price:
        lines.append(f'💵 행사가액: {price}원')

    # 누적 부여주식수
    if v := _get(kv, '8. Total grant after current grant'):
        lines.append(f'📦 누적부여: {v}주')

    # 결의기관
    if v := _get(kv, '5. Grant resolution body'):
        lines.append(f'📋 결의: {v}')

    # 행사기간
    start = _get(kv, 'Start date')
    end   = _get(kv, 'End date')
    if start:
        period = f'{start} ~ {end}' if end and end != start else start
        lines.append(f'📅 행사기간: {period}')

    return lines


def parse_treasury_disposal(kv: dict) -> list:
    """자기주식처분결정"""
    lines = []

    shares = _get(kv, '1. Shares to be disposed of', 'Shares to be disposed')
    price  = _get(kv, '2. Price of shares to be disposed of', 'Price of shares')
    amount = _get(kv, '3. Estimated disposal amount', 'Estimated disposal amount')

    if shares:
        lines.append(f'🔢 처분주식수: {shares}주')
    if price:
        lines.append(f'💵 처분가액: {price}원')
    if amount:
        lines.append(f'💰 처분예정금액: {_fmt_amount(amount)}원')

    # 처분기간
    start = _get(kv, '4. Scheduled disposal period', 'Start date')
    end   = _get(kv, 'End date')
    if start:
        period = f'{start} ~ {end}' if end and end != start else start
        lines.append(f'📅 처분기간: {period}')

    return lines


def parse_derivative_loss(kv: dict) -> list:
    """파생상품거래손실발생"""
    lines = []

    if v := _get(kv, '1. 파생상품 거래계약의 종류 및 내용', '거래계약의 종류'):
        lines.append(f'📋 거래종류: {_trunc(v, 50)}')

    loss   = _get(kv, '손실누계잔액(원)(기신고분 제외)', '손실누계잔액')
    ratio  = _get(kv, '자기자본대비(%)')
    if loss:
        ratio_str = f' (자기자본 대비 {ratio}%)' if ratio else ''
        lines.append(f'💸 손실누계: {_fmt_amount(loss)}원{ratio_str}')

    if v := _get(kv, '3. 손실발생 주요원인', '손실발생 주요원인'):
        lines.append(f'📋 원인: {_trunc(v, 70)}')

    if v := _get(kv, '4. 손실발생일자', '손실발생일자'):
        lines.append(f'📅 발생일: {v}')

    return lines


def parse_record_date(kv: dict) -> list:
    """주주명부폐쇄기간 또는 기준일 설정"""
    lines = []

    if v := _get(kv, '1. 기준일', '기준일'):
        lines.append(f'📅 기준일: {v}')

    if v := _get(kv, '3. 설정사유', '설정사유'):
        lines.append(f'📋 사유: {_trunc(v, 60)}')

    if v := _get(kv, '4. 이사회결의일', '이사회결의일'):
        lines.append(f'📋 결의일: {v}')

    return lines


def parse_rights_exercise(kv: dict) -> list:
    """전환청구권·신주인수권·교환청구권 행사"""
    lines = []

    # 구분 (전환/신주인수/교환)
    if v := _get(kv, '1. 구분', '구분'):
        lines.append(f'📋 구분: {_trunc(v, 60)}')

    # 행사주식수 + 발행주식 대비 (두 가지 키 구조 대응)
    shares = _get(kv, '2. 행사주식수 누계', '1. 교환청구권 행사주식수 누계', '행사주식수')
    ratio  = _get(kv, '발행주식총수 대비(%)', '-발행주식총수 대비 (%)', '발행주식총수 대비 (%)')
    if shares:
        ratio_str = f' (발행주식 대비 {ratio}%)' if ratio else ''
        lines.append(f'🔢 행사주식수: {shares}주{ratio_str}')

    # 관련공시
    if v := _get(kv, '※관련공시', '※ 관련공시', '관련공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

    return lines


def parse_large_holding_report(kv: dict) -> list:
    """주식등의대량보유상황보고서 — majorstock API 우선, HTML KV fallback."""
    lines = []
    rcept_no = kv.get('_rcept_no', '')

    # ── DART majorstock API 조회 ──────────────────────────────────────
    api = _fetch_dart_majorstock(rcept_no) if rcept_no else {}

    # 보고자명
    reporter = (api.get('repror') or '').strip()
    if not reporter:
        reporter = _get(kv, 'Reporting entity', '보고자', '보고자명', '성명') or ''
        reporter = re.sub(r'^[?\s]+', '', reporter).strip()
        if '?' in reporter:
            api_name = _fetch_dart_reporter(rcept_no)
            if api_name:
                reporter = api_name
    if reporter:
        lines.append(f'👤 보고자: {reporter}')

    # 보유목적 (API에 없으므로 HTML KV 사용)
    purpose = _get(kv, 'Purpose of holding', '보유목적', '주식등의보유목적', '보유 목적')
    if purpose and '?' not in purpose:
        lines.append(f'🎯 보유목적: {_trunc(purpose, 40)}')

    # 보고전/후 보유비율 — API 우선
    after_rt  = api.get('stkrt', '')       # 보고후 비율
    irds_rt   = api.get('stkrt_irds', '')  # 증감 (음수 가능)
    stkqy     = api.get('stkqy', '')       # 보유주식수
    ctr_stkrt = api.get('ctr_stkrt', '')   # 주요계약 비율

    if after_rt:
        try:
            after_f  = float(after_rt)
            irds_f   = float(irds_rt) if irds_rt else 0.0
            before_f = round(after_f - irds_f, 2)
            sign     = '+' if irds_f >= 0 else ''
            lines.append(f'📊 보고전: {before_f:.2f}% → 보고후: {after_f:.2f}% ({sign}{irds_f:.2f}%)')
        except ValueError:
            lines.append(f'📊 보유비율: {after_rt}%')
        if stkqy:
            lines.append(f'🔢 보유주식: {stkqy}주')
        if ctr_stkrt and ctr_stkrt != '0':
            lines.append(f'📋 주요계약: {ctr_stkrt}%')

    # 보고사유 — API 우선 (인코딩 깨짐 없음)
    reason = (api.get('report_resn') or '').strip()
    if not reason:
        reason = _get(kv, 'Reason for reporting', '보고사유', '보고 사유') or ''
        q_ratio = reason.count('?') / max(len(reason), 1)
        if q_ratio >= 0.1:
            reason = ''
    if reason:
        # '- ' 구분 목록을 bullet로 변환
        items = [re.sub(r'^-\s*', '', s).strip() for s in re.split(r'\n\s*-\s*|\s+-\s+', reason) if s.strip()]
        if len(items) > 1:
            lines.append('📋 보고사유:')
            for it in items[:4]:
                lines.append(f'  • {_trunc(it, 60)}')
        else:
            lines.append(f'📋 보고사유: {_trunc(reason, 100)}')

    # 이전보고일
    prev = _get(kv, 'Previous report', '직전보고서', '전보고서제출일', '이전보고')
    if prev:
        lines.append(f'📅 이전보고: {prev}')

    return lines


def parse_major_shareholder_change(kv: dict) -> list:
    """최대주주변경"""
    lines = []

    # 변경전/후 최대주주 — 값이 '변경전 최대주주' / '변경후 최대주주'인 KV 탐색
    items = list(kv.items())
    before_name = before_shares = before_ratio = ''
    after_name  = after_shares  = after_ratio  = ''

    for idx, (k, v) in enumerate(items):
        if k.startswith('_'):
            continue
        if '변경전 최대주주' in v and k not in ('-', '', '성명(법인명,조합명,기타단체명)'):
            before_name = k
            if idx + 1 < len(items):
                nk, nv = items[idx + 1]
                if re.match(r'^[\d,]+$', nk) and re.match(r'^[\d.]+$', nv):
                    before_shares, before_ratio = nk, nv
        elif '변경후 최대주주' in v and k not in ('-', '', '변경후'):
            after_name = k
            if idx + 1 < len(items):
                nk, nv = items[idx + 1]
                if re.match(r'^[\d,]+$', nk) and re.match(r'^[\d.]+$', nv):
                    after_shares, after_ratio = nk, nv

    if before_name or after_name:
        lines.append('🔄 최대주주 변경')
        if before_name:
            detail = f' ({before_shares}주 / {before_ratio}%)' if before_shares else ''
            lines.append(f'  변경전: {before_name}{detail}')
        if after_name:
            detail = f' ({after_shares}주 / {after_ratio}%)' if after_shares else ''
            lines.append(f'  변경후: {after_name}{detail}')

    # 변경사유
    if v := _get(kv, '2. 변경사유', '변경사유'):
        lines.append(f'📋 사유: {_trunc(v, 60)}')

    # 인수자금
    fund = _get(kv, '자기자금(원)')
    if fund and fund != '-':
        lines.append(f'💰 인수자금: {_fmt_amount(fund)}원 (자기자금)')

    # 인수목적
    if v := _get(kv, '3. 지분인수목적', '지분인수목적'):
        lines.append(f'📋 목적: {_trunc(v, 50)}')

    # 변경일자
    if v := _get(kv, '4. 변경일자', '변경일자'):
        lines.append(f'📅 변경일자: {v}')

    # 관련공시
    if v := _get(kv, '관련공시', '※ 관련공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

    return lines


def parse_insider_report(kv: dict) -> list:
    """임원ㆍ주요주주 특정증권등 소유상황보고서.

    이 보고서는 국/영문 이중언어 표라 영문 KV 키가 값과 정렬이 어긋난다
    (담당자 직위를 보고자 직위로, 변동전 수량을 현재보유로 오매칭). 따라서
    document.xml 원문 텍스트의 한글 라벨(직위명·발행주식 총수·변동전/증감/변동후)
    기준으로 파싱한다.
    """
    lines = []
    html = kv.get('_html', '')
    txt = re.sub(r'<[^>]+>', ' ', html)
    txt = re.sub(r'\s+', ' ', txt).strip()

    # 보고자 + 직위(직위명 — 담당자 '직 위'와 구분됨)
    reporter = (_get(kv, '보고자') or '').split('(')[0].strip()
    if not reporter:
        m_r = re.search(r'한\s*글\s+([가-힣]{2,5})', txt)
        reporter = m_r.group(1) if m_r else ''
    m_pos = re.search(r'직위명\s+([가-힣A-Za-z·]{2,15})', txt)
    position = m_pos.group(1).strip() if m_pos else ''
    if reporter:
        lines.append(f'👤 보고자: {reporter}' + (f' ({position})' if position else ''))

    # 발행주식 총수 (비율 계산 기준)
    m_tot = re.search(r'발행주식\s*총수\s+([\d,]+)', txt)
    total_issued = int(m_tot.group(1).replace(',', '')) if m_tot else 0

    def _ratio(n: int) -> str:
        return f' ({n / total_issued * 100:.2f}%)' if total_issued > 0 else ''

    # 소유 변동: '변동전 증감 변동후' 헤더 이후 세부표
    m_hdr = re.search(r'변동전\s+증감\s+변동후\s+(.*)', txt)
    detail = m_hdr.group(1) if m_hdr else ''

    # 보고사유 + 변동일 (첫 데이터 행: '[사유] YYYY.MM.DD ...')
    reason, change_date = '', ''
    m_row = re.match(r'\s*(.+?)\s+(\d{4}\.\d{2}\.\d{2})', detail)
    if m_row:
        reason = re.sub(r'\s*\([+\-]\)\s*$', '', m_row.group(1)).strip()
        change_date = m_row.group(2).replace('.', '-')

    # 변동전 / 변동후 (합계 행 우선, 없으면 첫 데이터 행)
    prev = after = None
    m_sum = re.search(r'합\s*계\s+([\d,]+)\s+[\d,]+\s+([\d,]+)', detail)
    if m_sum:
        prev  = int(m_sum.group(1).replace(',', ''))
        after = int(m_sum.group(2).replace(',', ''))
    else:
        m_d = re.search(r'\d{4}\.\d{2}\.\d{2}\s+\S+\s+([\d,]+)\s+[\d,]+\s+([\d,]+)', detail)
        if m_d:
            prev  = int(m_d.group(1).replace(',', ''))
            after = int(m_d.group(2).replace(',', ''))

    if prev is not None and after is not None:
        change = after - prev
        sign = '+' if change >= 0 else ''
        reason_str = f' · {reason}' if reason else ''
        lines.append(f'📊 증감: {sign}{change:,}주{reason_str}')
        if prev == 0:
            lines.append(f'📦 신규취득: {after:,}주{_ratio(after)}')
        else:
            arrow = '🔺' if change >= 0 else '🔻'
            lines.append(f'📦 보유: {prev:,}주{_ratio(prev)} {arrow} {after:,}주{_ratio(after)}')

    if change_date:
        lines.append(f'📅 변동일: {change_date}')

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


def parse_amendment(kv: dict) -> list:
    """
    [기재정정] 공시 전용 파서 — 변경된 항목만 추출.

    DART 정정 공시 KV 구조 (세 가지):
      패턴 A: "N. 섹션명 - 필드명": OLD  +  OLD: NEW
      패턴 B: "N. 섹션명": 부모헤더  +  "- 필드명: OLD": "- 필드명: NEW"
      패턴 C: "정정전_필드명": OLD  +  "정정후_필드명": NEW  (접두어 방식)
    """
    lines = []

    # ── 원공시 + 정정사유 ──────────────────────────────
    orig_doc  = _get(kv, '1. 정정관련 공시서류')
    orig_date = _get(kv, '2. 정정관련 공시서류제출일', '공시서류제출일')
    if orig_doc:
        lines.append(f'📄 {orig_doc}' + (f' ({orig_date})' if orig_date else ''))

    if v := _get(kv, '3. 정정사유', '정정사유'):
        # "정정전" / "정정후" 등 의미 없는 placeholder 값 제외
        v_clean = re.sub(r'\s+', '', v)
        if v_clean not in ('정정전', '정정후', '해당없음', '없음', '-', '—'):
            lines.append(f'📋 사유: {_trunc(v, 80)}')

    change_lines = []
    _MAX_CHANGES = 6  # 🔧 최대 출력 수
    # 설명성 필드 — 변경 전/후 비교 표시에서 제외
    _SKIP_FIELDS = {'중요사항', '비고', '기타사항', '첨부서류', '사항'}
    _MAX_VAL_LEN = 60  # 변경값 표시 최대 길이 (초과 시 truncate)

    # 헤더성 값 판별 — 컬럼 레이블이면 True (숫자 없고 괄호단위 포함 짧은 텍스트)
    def _is_label(v: str) -> bool:
        v = v.strip()
        if len(v) > 25 or re.search(r'\d{4}', v):
            return False
        if re.search(r'\(주\)|\(%\)|\(건\)|\(원\)', v):
            return True
        # 순수 텍스트 레이블 (숫자 전혀 없고 짧음)
        return not re.search(r'\d', v) and len(v) <= 15

    # new값이 field_name 자체와 동일하거나 포함 → 헤더 행
    def _is_header_row(field: str, old_v: str, new_v: str) -> bool:
        fn = re.sub(r'\s+', '', field)
        nv = re.sub(r'\s+', '', new_v)
        ov = re.sub(r'\s+', '', old_v)
        if fn == nv or fn == ov:
            return True
        if _is_label(old_v) and _is_label(new_v):
            return True
        if _is_label(old_v) and re.search(r'^\d[\d,]+$', new_v.replace(' ', '')):
            return True  # old=컬럼헤더, new=숫자 → 헤더+데이터 혼합 행
        if _is_label(old_v) and re.search(r'^\d{4}-\d{2}-\d{2}$', new_v.strip()):
            return True  # old=서브레이블(시작일 등), new=날짜값 → 중첩 테이블 행
        return False

    # ── 패턴 C: 정정전_* / 정정후_* 접두어 키 비교 (가장 신뢰도 높음) ──────
    before_keys = {k[4:]: v for k, v in kv.items() if k.startswith('정정전')}
    after_keys  = {k[4:]: v for k, v in kv.items() if k.startswith('정정후')}
    for field, old_v in before_keys.items():
        if len(change_lines) >= _MAX_CHANGES:
            break
        if _clean_amendment_field(field) in _SKIP_FIELDS:
            continue
        new_v = after_keys.get(field, '')
        old_c = re.sub(r'\s+', ' ', old_v).strip()
        new_c = re.sub(r'\s+', ' ', new_v).strip()
        if old_c and new_c and old_c != new_c and not _is_header_row(field, old_c, new_c):
            old_fmt = _fmt_amendment_val(field, _trunc(old_c, _MAX_VAL_LEN))
            new_fmt = _fmt_amendment_val(field, _trunc(new_c, _MAX_VAL_LEN))
            change_lines.append(f'🔧 {_clean_amendment_field(field)}: {old_fmt} → {new_fmt}')

    if change_lines:
        lines.extend(change_lines)
        return lines

    # ── 패턴 A / B: 정정항목 섹션 파싱 ──────────────────────────────────────
    items = list(kv.items())
    header_idx = next((i for i, (k, _) in enumerate(items) if k == '정정항목'), None)
    if header_idx is None:
        return lines

    i = header_idx + 1
    while i < len(items) and len(change_lines) < _MAX_CHANGES:
        k, val = items[i]

        # 패턴 A: "N. 섹션명 - 필드명": OLD  +  OLD: NEW
        m = re.match(r'^\d+\.\s+.+\s+-\s+(.+)$', k)
        if m:
            field_name = m.group(1).strip()
            if _clean_amendment_field(field_name) in _SKIP_FIELDS:
                i += 1
                continue
            new_val    = kv.get(val.strip(), '')
            old_clean  = val.strip()
            new_clean  = new_val.strip()
            if old_clean and new_clean and old_clean != new_clean:
                if not _is_header_row(field_name, old_clean, new_clean):
                    old_fmt = _fmt_amendment_val(field_name, _trunc(old_clean, _MAX_VAL_LEN))
                    new_fmt = _fmt_amendment_val(field_name, _trunc(new_clean, _MAX_VAL_LEN))
                    change_lines.append(f'🔧 {_clean_amendment_field(field_name)}: {old_fmt} → {new_fmt}')
            elif old_clean and not _is_label(old_clean):
                change_lines.append(f'🔧 {_clean_amendment_field(field_name)}: {_fmt_amendment_val(field_name, old_clean)}')
            i += 2
            continue

        # 패턴 B: "N. 섹션명" 부모 헤더 → 하위 "- 필드: old" / "- 필드: new"
        if re.match(r'^\d+\.\s+\S', k):
            j = i + 1
            while j < len(items) and len(change_lines) < _MAX_CHANGES:
                ck, cv = items[j]
                if not ck.startswith('-'):
                    break
                mo = re.match(r'^-\s*(.+?):\s*(.+)$', ck)
                mn = re.match(r'^-\s*(.+?):\s*(.+)$', cv)
                if mo and mn:
                    fname = mo.group(1).strip()
                    old_v = mo.group(2).strip()
                    new_v = mn.group(2).strip()
                    if _clean_amendment_field(fname) in _SKIP_FIELDS:
                        j += 1
                        continue
                    if old_v != new_v and not _is_header_row(fname, old_v, new_v):
                        old_fmt = _fmt_amendment_val(fname, _trunc(old_v, _MAX_VAL_LEN))
                        new_fmt = _fmt_amendment_val(fname, _trunc(new_v, _MAX_VAL_LEN))
                        change_lines.append(f'🔧 {_trunc(fname, 25)}: {old_fmt} → {new_fmt}')
                j += 1
            i = j
            continue

        break  # 정정 섹션 끝

    lines.extend(change_lines)
    return lines


def parse_tender_offer_result(kv: dict) -> list:
    """공개매수결과보고서 — HTML 인코딩 깨짐이 심해 HTML 원문에서 직접 추출."""
    lines = []
    html = kv.get('_html', '')
    if not html:
        return lines

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True)

    # 1주당 가격
    m = re.search(r'1주당\s*([\d,]+)\s*원', text)
    price = m.group(1) if m else None

    # 예정 매수수량 (공개매수 예정 주식수)
    planned = None
    for pat in [r'예정\s*주식[^\d]*([\d,]+)', r'공개매수\s*예정\s*주식[^\d]*([\d,]+)']:
        m = re.search(pat, text)
        if m:
            planned = m.group(1)
            break

    # 응모 주식수
    applied = None
    m = re.search(r'응모\s*주식[^\d]*([\d,]+)', text)
    if m:
        applied = m.group(1)

    # 실제 매수 주식수 (예정 이하)
    bought = None
    m = re.search(r'매수\s*주식[^\d]*([\d,]+)', text)
    if m:
        bought = m.group(1)

    # 공개매수 전/후 보유비율 — 소수점 포함 비율 두 개 추출
    ratios = re.findall(r'(\d{1,2}\.\d{1,2})\s*%?', text)
    before_ratio = after_ratio = None
    if len(ratios) >= 2:
        # 보통 전→후 순서로 두 번 나옴
        before_ratio, after_ratio = ratios[0], ratios[1]

    # 매수대리인 (NH투자증권 등 — 인코딩 살아남는 편)
    agent = None
    m = re.search(r'(NH투자증권|한국투자증권|미래에셋|삼성증권|KB증권|신한투자증권|하나증권|키움증권)', text)
    if m:
        agent = m.group(1)

    # 공개매수자 (KV에서 가능한 값 탐색)
    buyer = None
    for k, v in kv.items():
        if k.startswith('_'):
            continue
        # 값에 '공개매수자' 레이블이 있거나 키에 포함
        if '공개매수자' in k and len(v) > 1 and not re.search(r'\d{4}', v):
            buyer = v
            break

    lines.append('📢 공개매수 결과')
    if buyer:
        lines.append(f'🏢 공개매수자: {buyer}')
    if agent:
        lines.append(f'🏦 매수대리인: {agent}')
    if price:
        lines.append(f'💰 매수가격: 1주당 {price}원')
    if applied:
        lines.append(f'📥 응모수량: {applied}주')
    # 예정 = 실제매수면 하나만 표시
    if bought and planned and bought == planned.replace(',', ''):
        lines.append(f'✅ 실제매수: {int(bought):,}주 (예정수량 전량)')
    else:
        if planned:
            lines.append(f'📋 예정수량: {planned}주')
        if bought:
            lines.append(f'✅ 실제매수: {int(bought):,}주')

    # ── 보유자별 보유 현황 테이블 파싱 ─────────────────────────────────────
    holder_lines = []
    _IS_NUM   = re.compile(r'^[\d,]+$')
    _IS_RATIO = re.compile(r'^\d{1,2}\.\d{1,2}$')

    for table in soup.find_all('table'):
        headers = [td.get_text(strip=True) for td in table.find_all(['th', 'td'])[:8]]
        header_text = ' '.join(headers)
        if not re.search(r'명칭|보유주|비율', header_text):
            continue

        for tr in table.find_all('tr'):
            cells = [td.get_text(strip=True) for td in tr.find_all('td')]
            if len(cells) < 3:
                continue
            name = cells[0]
            if not re.search(r'[가-힣?]', name) or _IS_NUM.match(name) or _IS_RATIO.match(name):
                continue
            if len(name) <= 3 and '(' not in name:
                continue

            nums     = [c.replace(',', '') for c in cells[1:] if _IS_NUM.match(c.replace(',', ''))]
            ratios_r = [c for c in cells[1:] if _IS_RATIO.match(c)]

            before_sh  = int(nums[0]) if len(nums) > 0 else None
            bought_sh  = int(nums[1]) if len(nums) > 1 else None
            after_sh   = int(nums[2]) if len(nums) > 2 else None
            b_ratio    = ratios_r[0]  if len(ratios_r) > 0 else ''
            a_ratio    = ratios_r[1]  if len(ratios_r) > 1 else ''

            name_clean = re.sub(r'[^가-힣\w\s\(\)]', '', name).strip() or name

            # 형식: • 에스폼(주): 5,703,203주 → 6,903,203주 (38.80%→46.96%)
            share_str = ''
            if before_sh is not None and after_sh is not None:
                share_str = f'{before_sh:,}주 → {after_sh:,}주'
            elif before_sh is not None:
                share_str = f'{before_sh:,}주'

            ratio_str = ''
            if b_ratio and a_ratio:
                ratio_str = f' ({b_ratio}%→{a_ratio}%)'
            elif b_ratio:
                ratio_str = f' ({b_ratio}%)'

            bought_str = ''
            if bought_sh and bought_sh > 0:
                bought_str = f' [+{bought_sh:,}주]'

            if share_str:
                holder_lines.append(f'  • {name_clean}: {share_str}{bought_str}{ratio_str}')

        if holder_lines:
            break

    if holder_lines:
        lines.append('👥 보유자별 현황')
        lines.extend(holder_lines)
    elif before_ratio and after_ratio:
        # 테이블 파싱 실패 시 전체 지분율만 표시
        lines.append(f'📊 지분율: {before_ratio}% → {after_ratio}%')

    return lines


def parse_share_pledge(kv: dict) -> list:
    """주식담보제공계약체결 — 담보제공자, 금액, 기간 핵심 요약."""
    lines = []

    # 담보제공자
    provider = _get(kv,
        '명칭(성명, 법인명, 조합명, 단체명)',
        '성명(명칭)', '담보제공자', '명칭')
    if provider:
        lines.append(f'🏢 담보제공자: {provider}')

    # 보유 지분
    shares = _get(kv, '소유 주식 수(주)', '소유주식수(주)')
    ratio  = _get(kv, '지분율(%)', '지분율')
    if shares:
        ratio_str = f' ({ratio}%)' if ratio else ''
        lines.append(f'📊 보유지분: {int(shares.replace(",", "")):,}주{ratio_str}')

    # 채무금액 / 담보설정금액
    debt = _get(kv, '2. 채무(차입)금액 총액(원)', '채무(차입)금액 총액(원)', '채무금액')
    coll = _get(kv, '3. 담보설정금액 총액(원)', '담보설정금액 총액(원)', '담보설정금액')
    if debt:
        lines.append(f'💸 채무금액: {_fmt_amount(debt)}원')
    if coll:
        lines.append(f'🔒 담보설정: {_fmt_amount(coll)}원')

    # 담보제공 주식수 (누적)
    pledge_sh = _get(kv, '누적 담보제공 주식 총수(주)', '담보제공주식수(주)')
    if pledge_sh:
        lines.append(f'📌 담보주식: {int(pledge_sh.replace(",", "")):,}주')

    # 담보 종류
    kind = _get(kv, '담보권 종류', '담보종류')
    if kind:
        lines.append(f'📋 담보종류: {kind}')

    # 담보제공기간
    start = _get(kv, '시작일', '담보제공기간시작일')
    end   = _get(kv, '종료일', '담보제공기간종료일')
    if start and end:
        lines.append(f'📅 담보기간: {start} ~ {end}')
    elif start or end:
        lines.append(f'📅 담보기간: {start or end}')

    # 계약 체결일
    if v := _get(kv, '5. 담보권 설정계약 체결일(당해 건)', '담보권 설정계약 체결일'):
        lines.append(f'📝 계약체결일: {v}')

    return lines


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


def parse_bonus_issue(kv: dict) -> list:
    """무상증자결정 — 배정비율·신주수·기준일·상장일·발행주식 증가.

    국/영문 이중언어 표라 영문 KV 키 정렬이 어긋남(제출문 헤더 노이즈 포함)
    → 원문 텍스트의 한글 라벨 기준 파싱(임원보고서 파서와 동일 접근).
    """
    lines = []
    txt = re.sub(r'<[^>]+>', ' ', kv.get('_html', ''))
    txt = re.sub(r'\s+', ' ', txt).strip()

    def _kdate(s: str) -> str:
        m = re.search(r'(\d{4})\D+(\d{1,2})\D+(\d{1,2})', s or '')
        return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}' if m else (s or '').strip()

    # 1주당 배정비율 — 무상증자의 핵심
    m = re.search(r'1주당\s*신주배정\s*주식수\s*보통주식?\s*\(주\)\s*([\d.]+)', txt)
    if m:
        lines.append(f'🎁 무상배정: 1주당 {m.group(1)}주')

    # 신주 수 (보통 + 기타)
    m = re.search(r'신주의\s*종류와\s*수\s*보통주식?\s*\(주\)\s*([\d,]+)'
                  r'(?:\s*기타주식\s*\(주\)\s*([\d,\-]+))?', txt)
    if m:
        s = f'📊 신주: 보통 {m.group(1)}주'
        etc = (m.group(2) or '').strip()
        if etc and etc not in ('-', '0'):
            s += f' + 기타 {etc}주'
        lines.append(s)

    # 배정기준일 · 상장예정일
    m  = re.search(r'신주배정기준일\s*([^\s].{0,20}?일)', txt)
    m2 = re.search(r'상장\s*예정일\s*([^\s].{0,20}?일|-)', txt)
    if m or m2:
        parts = []
        if m:
            parts.append(f'배정기준 {_kdate(m.group(1))}')
        if m2 and m2.group(1) != '-':
            parts.append(f'상장 {_kdate(m2.group(1))}')
        lines.append('📅 ' + ' | '.join(parts))

    # 발행주식 전→후 (배정내역 표, 보통주 자기주식 제외 행)
    m = re.search(r'보통주\(자기주식\s*제외\)\s*([\d,]+)\s*([\d,]+|-)\s*([\d,]+)', txt)
    if m:
        try:
            pre  = int(m.group(1).replace(',', ''))
            post = int(m.group(3).replace(',', ''))
            pct  = (post - pre) / pre * 100 if pre else 0
            lines.append(f'📦 발행주식: {pre:,} → {post:,}주 (+{pct:.1f}%)')
        except ValueError:
            pass

    m = re.search(r'이사회결의일\(?결정일\)?\s*([^\s].{0,20}?일)', txt)
    if m:
        lines.append(f'📅 결의일: {_kdate(m.group(1))}')

    return lines


def parse_misc_mgmt(kv: dict) -> list:
    """기타주요경영사항(자율공시) — 주요내용이 곧 공시의 본체.

    구조: 1.제출사유 / 2.주요내용 / 3.결정(발생)일자 / 4.기타(관련공시).
    제출사유는 공시 제목 괄호에 이미 노출되므로 생략, 주요내용을 넉넉히(500자,
    문장경계) 표시. 번호목록 구조면 _parse_numbered_body로 분리.
    """
    lines = []

    body = _get(kv, '2. 주요내용', '주요내용') or ''
    stripped = _strip_disclaimer(body).strip()
    if stripped:
        bullets = _parse_numbered_body(stripped)
        if bullets and len(bullets) >= 2:
            lines.extend(bullets)
        else:
            clean = re.sub(r'^[\-·•]\s*', '', re.sub(r'\s+', ' ', stripped)).strip()
            # 주요내용이 공시 본체 → 사실상 전문 표시 (2000자 초과 극단 케이스만 절단,
            # 4000자 초과 발송은 managers._split_text가 분할 처리)
            lines.append(f'📋 {_trunc_clean(clean, 2000)}')

    if v := _get(kv, '3. 결정(발생)일자', '결정(발생)일자', '결정일자', '발생일자'):
        lines.append(f'📅 결정일: {v}')

    # 관련공시 (4.기타 값 안의 '※ 관련 공시 - 날짜. 제목 - ...' 목록 → 최근 2건)
    etc = _get(kv, '4. 기타 투자판단에 참고할 사항', '기타 투자판단에 참고할 사항') or ''
    rel = re.findall(r'(\d{4}\.\d{2}\.\d{2})\.?\s*([가-힣A-Za-z0-9()·\s]{4,40}?)(?=\s*-\s*\d{4}\.|\s*$)', etc)
    if rel:
        shown = ' · '.join(f'{d} {t.strip()}' for d, t in rel[-2:])
        lines.append(f'🔗 관련: {_trunc(shown, 90)}')

    return lines


def parse_lawsuit(kv: dict) -> list:
    """소송등의제기ㆍ신청 / 판결ㆍ결정 — 사건명·원고·청구금액·내용·법원·대책"""
    lines = []

    if v := _get(kv, '사건의 명칭', '사건명'):
        lines.append(f'⚖️ 사건: {_trunc(v, 70)}')

    if v := _get(kv, '원고ㆍ신청인', '원고·신청인', '원고(신청인)', '원고'):
        lines.append(f'👤 원고: {_trunc(_clean_party(v), 50)}')

    # 청구금액 + 자기자본 대비
    amount = _get(kv, '청구금액(원)', '소송가액(원)', '청구금액', '소송가액')
    ratio  = _get(kv, '자기자본대비(%)', '자기자본 대비(%)')
    if amount and re.search(r'\d', amount):
        m = re.search(r'([\d,]{4,})', amount)
        if m:
            ratio_str = f' (자기자본 대비 {ratio}%)' if ratio else ''
            lines.append(f'💰 청구금액: {_fmt_amount(m.group(1))}원{ratio_str}')

    if v := _get(kv, '판결ㆍ결정내용', '판결·결정내용', '판결내용', '청구내용', '신청취지'):
        body = _trunc_clean(re.sub(r'\s+', ' ', v), 150)
        lines.append(f'📋 내용: {body}')

    if v := _get(kv, '관할법원', '법원'):
        lines.append(f'🏛 관할: {_trunc(v, 40)}')

    if v := _get(kv, '향후대책', '향후 대책'):
        plan = _trunc_clean(re.sub(r'\s+', ' ', v), 120)
        lines.append(f'🧭 대책: {plan}')

    if v := _get(kv, '제기일자', '판결일자', '확인일자', '접수일자'):
        lines.append(f'📅 일자: {_clean_date(v)}')

    return lines


def parse_embezzlement(kv: dict) -> list:
    """횡령ㆍ배임 혐의발생 / 사실확인 — 대상자·혐의금액·내용·진행단계"""
    lines = []

    person   = _get(kv, '사고자', '고소ㆍ고발 대상자', '혐의자', '대상자')
    relation = _get(kv, '회사와의 관계', '직위')
    if person:
        rel = f' ({relation})' if relation and relation != person else ''
        lines.append(f'👤 대상: {_trunc(person, 40)}{rel}')

    amount = _get(kv, '혐의발생금액(원)', '횡령등 금액(원)', '혐의발생금액', '횡령등금액')
    ratio  = _get(kv, '자기자본대비(%)', '자기자본 대비(%)')
    if amount and re.search(r'\d', amount):
        m = re.search(r'([\d,]{4,})', amount)
        if m:
            ratio_str = f' (자기자본 대비 {ratio}%)' if ratio else ''
            lines.append(f'💸 혐의금액: {_fmt_amount(m.group(1))}원{ratio_str}')

    if v := _get(kv, '혐의내용', '사고내용', '확인내용'):
        body = _trunc_clean(re.sub(r'\s+', ' ', v), 150)
        lines.append(f'📋 혐의: {body}')

    if v := _get(kv, '진행상황', '조치내용', '향후대책'):
        action = _trunc_clean(re.sub(r'\s+', ' ', v), 100)
        lines.append(f'🧭 조치: {action}')

    if v := _get(kv, '확인일자', '발생일자', '혐의발생일'):
        lines.append(f'📅 확인일: {_clean_date(v)}')

    return lines


def parse_market_measure(kv: dict) -> list:
    """상장폐지·관리종목·상장적격성 등 시장조치 — 대상·사유·일자·근거"""
    lines = []

    if v := _get(kv, '대상종목', '종목명'):
        lines.append(f'📋 대상: {_trunc(v, 50)}')

    if v := _get(kv, '지정사유', '해제사유', '폐지사유', '결정사유', '선정사유', '사유'):
        reason = _trunc_clean(re.sub(r'\s+', ' ', v), 150)
        lines.append(f'🚨 사유: {reason}')

    for label, keys in (('📅 지정일', ('지정일',)),
                        ('📅 해제일', ('해제일',)),
                        ('📅 폐지일', ('폐지일', '상장폐지일')),
                        ('🕐 정리매매', ('정리매매',))):
        if v := _get(kv, *keys):
            lines.append(f'{label}: {_trunc(v, 60)}')

    if v := _get(kv, '근거규정', '근거'):
        lines.append(f'📋 근거: {_trunc(v, 60)}')

    if v := _get(kv, '5.기타', '기타'):
        lines.extend(_parse_etc_field(v)[:4])

    # KRX 기타시장안내형 폴백 — 정형 필드가 없으면 제목/내용 KV,
    # 그마저 없으면(표 없는 산문 문서) 원문 텍스트의 '제목 :' 이후를 추출
    if not lines:
        title = _get(kv, '제목')
        body  = _get(kv, '내용')
        if not title and not body:
            raw = kv.get('_html', '')
            if raw:
                no_css = re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', raw,
                                flags=re.DOTALL | re.IGNORECASE)
                txt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', no_css)).strip()
                m = re.search(r'제\s*목\s*[:：]\s*(.+)$', txt)
                if m:
                    seg = m.group(1).strip()
                    head, _sep, rest = seg.partition(')')   # 제목은 대개 괄호로 종결
                    if rest.strip():
                        title, body = (head + ')').strip(), rest.strip()
                    else:
                        body = seg
        if title:
            t = _trunc_clean(re.sub(r'\s+', ' ', title), 100)
            lines.append(f'📋 {t}')
        if body:
            b = _trunc_clean(re.sub(r'\s+', ' ', body), 500)
            lines.append(f'  {b}')

    return lines


# 공시 제목 키워드 → 카테고리 파서 매핑
# ※ 순서 중요: 구체적인 타입을 먼저, 일반적인 타입을 나중에
_PARSER_MAP = [
    (['유무상증자'],                         parse_combined_ci),
    (['유상증자'],                          parse_rights_offering),
    (['무상증자'],                          parse_bonus_issue),
    (['단일판매', '공급계약체결', '수주'],   parse_contract),
    (['전환사채', '신주인수권부사채'],        parse_cb),
    (['투자판단관련주요경영사항'],           parse_mgmt_event),
    (['기타주요경영사항'],                   parse_misc_mgmt),
    (['임원ㆍ주요주주', '임원·주요주주'],     parse_insider_report),
    (['거래정지', '매매거래정지'],           parse_trading_halt),
    (['상장폐지', '관리종목', '상장적격성'],   parse_market_measure),
    (['소송'],                                parse_lawsuit),
    (['횡령', '배임'],                         parse_embezzlement),
    (['권리락'],                             parse_ex_rights),
    (['최대주주변경'],                        parse_major_shareholder_change),
    (['주주명부폐쇄', '기준일설정'],           parse_record_date),
    (['전환청구권', '신주인수권', '교환청구권행사'], parse_rights_exercise),
    (['채무보증'],                            parse_debt_guarantee),
    (['주주총회소집결의', '주주총회소집공고'], parse_agm_notice),
    (['주식매수선택권'],                       parse_stock_option),
    (['자기주식처분'],                         parse_treasury_disposal),
    (['파생상품거래손실'],                     parse_derivative_loss),
    (['타법인주식', '출자증권취득'],           parse_equity_acquisition),
    (['기업설명회', 'IR개최'],               parse_ir_event),
    (['주주총회결과'],                        parse_agm_result),
    (['대표이사변경', '임원변경'],            parse_executive_change),
    (['본점소재지변경'],                      parse_hq_relocation),
    (['사외이사의선임', '사외이사선임'],       parse_outside_director),
    (['기업가치제고'],                         parse_value_enhancement),
    (['잠정실적', '잠정영업실적', '영업(잠정)실적'], parse_preliminary_earnings),
    (['신탁계약해지결정'],                       parse_trust_termination_decision),
    (['신탁계약해지결과'],                       parse_trust_termination),
    (['자기주식취득신탁', '자기주식취득결정'],   parse_treasury_acquisition),
    (['대량보유상황보고서'],                      parse_large_holding_report),
    (['공개매수결과보고서', '공개매수청약'],       parse_tender_offer_result),
    (['주식담보제공'],                             parse_share_pledge),
]

# 상세 파싱 불필요 공시 유형 — 헤더만 표시 (parse_all_fields fallback 방지)
_SKIP_DETAIL_TYPES = frozenset([
    '대규모기업집단현황', '기업지배구조보고서',
    # 정기보고서류 — 수천 개 KV + 인코딩 깨짐, 헤더만 표시
    '사업보고서', '반기보고서', '분기보고서', '감사보고서',
])
