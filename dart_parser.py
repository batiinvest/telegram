"""
dart_parser.py — DART 공시 원문 구조화 파서

공시 HTML에서 전체 필드를 추출하여 텔레그램 메시지용 텍스트로 반환.
파싱 실패 / HTML 없으면 빈 문자열 반환 (graceful fallback).

[구조]
  1. HTML 가져오기 (_fetch_html)
  2. KV 추출 (_build_kv) — 공시 HTML 테이블의 모든 행 파싱
  3. 범용 파서 (parse_all_fields) — 전체 필드 정리해서 출력
  4. 카테고리별 파서 (parse_*) — 추후 분류별로 양식 정의
"""

import re
import logging
from bs4 import BeautifulSoup
from managers import global_session as _session

log = logging.getLogger(__name__)

_DESKTOP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)


# ══════════════════════════════════════════════
#  HTML 가져오기
# ══════════════════════════════════════════════

def _fix_dart_utf8(content: bytes) -> bytes:
    """
    DART 서버의 UTF-8 변환 버그 복구.
    0xEC / 0xED 리드 바이트가 0x3F(?)로 손상된 3바이트 UTF-8 시퀀스를 복원.
    """
    buf = bytearray()
    i   = 0
    n   = len(content)
    while i < n:
        b = content[i]
        if (b == 0x3F
                and i + 2 < n
                and 0x80 <= content[i+1] <= 0xBF
                and 0x80 <= content[i+2] <= 0xBF):
            recovered = False
            for lead in (0xEC, 0xED):
                cand = bytes([lead, content[i+1], content[i+2]])
                try:
                    ch = cand.decode('utf-8')
                    if '가' <= ch <= '힣':
                        buf.extend(cand)
                        i += 3
                        recovered = True
                        break
                except (UnicodeDecodeError, ValueError):
                    pass
            if not recovered:
                buf.append(b)
                i += 1
        else:
            buf.append(b)
            i += 1
    return bytes(buf)


def _detect_encoding(content: bytes, headers: dict) -> str:
    """
    인코딩 감지 우선순위:
    1. XML 선언
    2. HTML meta charset
    3. Content-Type 응답 헤더
    4. 기본 euc-kr
    """
    if content[:6] in (b'<?xml ', b'<?XML '):
        m = re.search(rb'encoding=["\']([^"\']+)["\']', content[:300])
        if m:
            return m.group(1).decode('ascii', errors='ignore').lower()

    sample = content[:4096].lower()
    if b'charset=utf-8' in sample or b'charset="utf-8"' in sample:
        return 'utf-8'
    if b'charset=euc-kr' in sample or b'charset="euc-kr"' in sample:
        return 'euc-kr'

    ct = headers.get('Content-Type', '')
    if 'charset=' in ct.lower():
        enc = ct.lower().split('charset=')[-1].strip().split(';')[0].strip().rstrip('"\'')
        if enc:
            return enc

    return 'euc-kr'


def _fetch_html(rcept_no: str) -> str | None:
    """DART 공시 본문 HTML 가져오기."""
    headers = {'User-Agent': _DESKTOP_UA}
    base    = 'http://dart.fss.or.kr'
    try:
        idx_url = f'{base}/dsaf001/main.do?rcpNo={rcept_no}'
        idx = _session.get(idx_url, headers=headers, timeout=8)
        if idx.status_code != 200:
            log.warning(f'[DART 파서] 목차 fetch 실패 (status {idx.status_code}): {idx_url}')
            return None

        m = re.search(r'viewDoc\("(\d+)",\s*"(\d+)"', idx.content.decode('utf-8', errors='replace'))
        if not m:
            log.debug(f'[DART 파서] viewDoc 패턴 없음 ({rcept_no})')
            return None

        rcp_no_found = m.group(1)
        dcm_no       = m.group(2)

        doc_url = (f'{base}/report/viewer.do'
                   f'?rcpNo={rcp_no_found}&dcmNo={dcm_no}'
                   f'&eleId=0&offset=0&length=0&dtd=HTML')
        doc = _session.get(doc_url, headers=headers, timeout=8)
        if doc.status_code != 200:
            log.warning(f'[DART 파서] 본문 fetch 실패 (status {doc.status_code}): {doc_url}')
            return None

        raw = doc.content
        enc = _detect_encoding(raw, doc.headers)
        is_xml_utf8 = (enc.lower().replace('-', '').replace('_', '') == 'utf8'
                       and raw[:6] in (b'<?xml ', b'<?XML '))

        if is_xml_utf8:
            # DART XML 문서: "encoding=utf-8" 선언이지만 0xEC/0xED 리드 바이트가
            # 0x3F(?)로 손상되어 있음 → _fix_dart_utf8 복구 후 errors='replace' 디코딩.
            # CP949 시도는 오히려 한자가 섞인 잘못된 결과를 내므로 사용 안 함.
            fixed = _fix_dart_utf8(raw)
            log.debug(f'[DART 파서] XML/utf-8 모드 ({rcept_no})')
            return fixed.decode('utf-8', errors='replace')

        # HTML 또는 non-utf8 문서: HTTP Content-Type 기준 인코딩 우선 시도
        candidates = [enc, 'cp949', 'euc-kr']
        for try_enc in candidates:
            try:
                return raw.decode(try_enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode('utf-8', errors='replace')

    except Exception as e:
        log.warning(f'[DART 파서] HTML 요청 실패 ({rcept_no}): {e}')
    return None


# ══════════════════════════════════════════════
#  KV 추출
# ══════════════════════════════════════════════

def _build_kv(html: str) -> dict:
    """
    테이블 모든 행에서 key→value 매핑 추출.
    - 2셀: (key, val)
    - 3셀: (key, val, unit) — unit이 짧으면 val에 합침
    - 4셀: (key1, val1, key2, val2)
    - 5셀+: 짝수 쌍으로 처리
    기재정정 공시의 경우 '정정전'/'정정후' 접두 키도 그대로 저장.
    """
    import warnings
    try:
        from bs4 import XMLParsedAsHTMLWarning
        warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)
    except ImportError:
        pass

    # DART XML 엔티티 정규화 (&cr; = 줄바꿈, &nbsp; 등)
    html = (html
            .replace('&cr;',   ' ')
            .replace('&CR;',   ' ')
            .replace('&nbsp;', ' ')
            .replace('\xa0',   ' '))

    soup = BeautifulSoup(html, 'html.parser')
    kv: dict = {}

    def _cell_text(cell) -> str:
        """셀 텍스트 추출 + 잔여 XML 엔티티 정리."""
        t = cell.get_text(' ', strip=True)
        t = re.sub(r'&[a-zA-Z]{1,8};', ' ', t)
        return re.sub(r'\s+', ' ', t).strip()

    def _fmt_aunit(aunit_val: str) -> str:
        """AUNITVALUE 포맷: YYYYMMDD→날짜, 순수숫자→천단위 구분."""
        v = (aunit_val or '').strip()
        if not v or v in ('-', '0', ''):
            return ''
        if re.match(r'^\d{8}$', v):          # 날짜
            return f'{v[:4]}-{v[4:6]}-{v[6:]}'
        if re.match(r'^-?\d{5,}$', v):       # 큰 숫자
            try:
                return f'{int(v):,}'
            except ValueError:
                pass
        return v

    for row in soup.find_all('tr'):
        tds = row.find_all(['td', 'th'])
        tus = row.find_all('tu')   # DART XML 전용 값 태그

        tes = row.find_all('te')   # DART XML 숫자/텍스트 입력 태그

        _SKIP = {'-', '없음', 'N/A', '해당없음', '해당 없음', '해당없음(√)'}

        if tus or tes:
            if not tds:
                # ── TE-only 행: TD 없는 데이터 행 (배정대상 테이블 등) ──
                # ACODE="PART" 배정대상자, ACODE="ALL_CNT" 배정주식수 처리
                row_acodes = {}
                for te in tes:
                    acode = (te.get('acode') or te.get('ACODE') or '').strip().upper()
                    val   = _cell_text(te)
                    if acode and val and val not in _SKIP:
                        row_acodes[acode] = val
                if 'PART' in row_acodes:
                    i = 0
                    while f'_allottee_{i}' in kv:
                        i += 1
                    kv[f'_allottee_{i}'] = row_acodes['PART']
                    if 'ALL_CNT' in row_acodes:
                        kv[f'_allot_cnt_{i}'] = row_acodes['ALL_CNT']
            else:
                # ── DART XML 방식: <TD ENG="..."> + <TU AUNITVALUE="..."> / <TE ACODE="..."> ──
                for td in tds:
                    eng   = (td.get('eng') or td.get('ENG') or '').strip()
                    label = eng if eng else _cell_text(td)
                    if not label:
                        continue

                    # 1순위: TU (날짜·선택값, AUNITVALUE)
                    val = ''
                    for tu in tus:
                        av  = (tu.get('aunitvalue') or tu.get('AUNITVALUE') or '').strip()
                        val = _fmt_aunit(av) if av else _cell_text(tu)
                        if val and val not in _SKIP:
                            break
                        val = ''

                    # 2순위: TE (숫자·텍스트 입력값)
                    if not val:
                        for te in tes:
                            val = _cell_text(te)
                            if val and val not in _SKIP:
                                break
                            val = ''

                    if val:
                        kv[label] = val
        else:
            # ── 기존 방식: <TD> 전용 행 ──
            cells = [_cell_text(c) for c in tds]
            cells = [c for c in cells if c]
            n = len(cells)
            if n == 2:
                if cells[0] and cells[1]:
                    kv[cells[0]] = cells[1]
            elif n == 3:
                if cells[0] and cells[1]:
                    if len(cells[2]) <= 6:
                        kv[cells[0]] = (cells[1] + ' ' + cells[2]).strip()
                    else:
                        kv[cells[0]] = cells[1]
                        if cells[1] and cells[2]:
                            kv[cells[1]] = cells[2]
            elif n == 4:
                if cells[0] and cells[1]:
                    kv[cells[0]] = cells[1]
                if cells[2] and cells[3]:
                    kv[cells[2]] = cells[3]
            elif n >= 5:
                for i in range(0, n - 1, 2):
                    if cells[i] and cells[i + 1]:
                        kv[cells[i]] = cells[i + 1]

    return kv


def _get(kv: dict, *keys: str) -> str | None:
    """키 후보들 중 첫 번째로 부분일치하는 값 반환.
    기재정정 공시에서는 '정정후' 키를 우선 반환."""
    for key in keys:
        for k, v in kv.items():
            if key in k and '정정후' in k:
                clean = re.sub(r'\s+', ' ', v).strip()
                if clean and clean not in ('-', '—', '없음', 'N/A'):
                    return clean
        for k, v in kv.items():
            if key in k and '정정전' not in k:
                clean = re.sub(r'\s+', ' ', v).strip()
                if clean and clean not in ('-', '—', '없음', 'N/A'):
                    return clean
    return None


def _trunc(text: str, limit: int = 80) -> str:
    return text[:limit] + '…' if len(text) > limit else text


# ══════════════════════════════════════════════
#  범용 파서 — 전체 필드 추출
# ══════════════════════════════════════════════

# 출력 제외할 키 패턴 (서명·연락처 등 노이즈)
_SKIP_KEY_PATTERNS = [
    '날인', '서명', '인(印)', '확인자', '위임장',
    '본점소재지', '법인등록번호', '사업자등록번호',
    '전화번호', '팩스번호', '홈페이지', 'E-mail',
    '작성책임자', '공시담당자', '담당부서', '대표이사',
    '주민등록번호', '주소',
]

# 출력 제외할 값
_SKIP_VALUES = frozenset([
    '-', '—', '없음', 'N/A', '해당없음', '해당 없음', '없 음',
    'n/a', '해당사항없음', '해당사항 없음', '미정', '추후 결정', '추후결정',
    '□', '■', '○', '●', '해당없음(√)', '√', '', ' ',
])

# 값 최대 길이 (Telegram 메시지 길이 고려)
_MAX_VAL_LEN = 100

# 텔레그램 메시지 전체 최대 필드 수
_MAX_FIELDS = 20


def parse_all_fields(kv: dict) -> list:
    """
    범용 파서: DART HTML 테이블에서 추출한 전체 KV를 정리해 반환.

    - 노이즈 키/값 제거 (서명, 연락처, 빈값 등)
    - 중복 값 제거
    - 너무 긴 값 truncate
    - 최대 _MAX_FIELDS 개 출력 (Telegram 길이 제한 고려)
    """
    lines = []
    seen_vals: set = set()

    for k, v in kv.items():
        k = re.sub(r'\s+', ' ', k).strip()
        v = re.sub(r'\s+', ' ', v).strip()

        # 빈 값 / 의미없는 값 제외
        if not v or v in _SKIP_VALUES:
            continue
        # 키가 너무 짧거나 없으면 제외
        if not k or len(k) < 2:
            continue
        # 노이즈 키 제외
        if any(p in k for p in _SKIP_KEY_PATTERNS):
            continue
        # 키와 값이 동일한 경우 (헤더가 KV로 잘못 추출된 경우)
        if k == v:
            continue
        # 중복 값 제외 (짧은 값이 여러 키로 반복되는 경우)
        v_norm = v.lower().strip()
        if v_norm in seen_vals and len(v) < 15:
            continue
        seen_vals.add(v_norm)

        # 너무 긴 값 truncate
        if len(v) > _MAX_VAL_LEN:
            v = v[:_MAX_VAL_LEN] + '…'

        lines.append(f'{k}: {v}')

        if len(lines) >= _MAX_FIELDS:
            break

    return lines


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

    # 신주식수
    if v := _get(kv, '1. Class and number of new shares'):
        lines.append(f'🔢 신주식수: {v}주')

    # 발행가액 + 할인율
    price    = _get(kv, '6. Issuing price of new shares', 'Issuing price')
    discount = _get(kv, '7-2. Discount or premium ratio', 'Discount or premium ratio (%)')
    if price:
        disc_str = f' (할인율 {discount}%)' if discount else ''
        lines.append(f'💵 발행가액: {price}원{disc_str}')

    # 기준주가
    if v := _get(kv, '7. Base stock price', 'Base stock price: Lower'):
        lines.append(f'📊 기준주가: {v}원')

    # 조달금액 (목적별)
    for fk in _FUND_KEYS:
        if v := _get(kv, fk):
            lines.append(f'💰 조달금액: {_fmt_amount(v)}원')
            break

    # 납입일
    if v := _get(kv, 'Payment date'):
        lines.append(f'📅 납입일: {v}')

    # 상장예정일
    if v := _get(kv, 'Scheduled listing date'):
        lines.append(f'📅 상장예정: {v}')

    # 증자방식
    if v := _get(kv, '5. Capital increase method'):
        lines.append(f'📋 방식: {_CI_METHOD.get(v, v)}')

    # 이사회결의일
    if v := _get(kv, 'Board resolution date'):
        lines.append(f'📋 결의일: {v}')

    # 제3자 배정대상자 (PART/ALL_CNT 행에서 수집)
    allottees = []
    i = 0
    while f'_allottee_{i}' in kv:
        name = kv[f'_allottee_{i}']
        cnt  = kv.get(f'_allot_cnt_{i}', '')
        allottees.append(f'{name} ({cnt}주)' if cnt else name)
        i += 1
    if allottees:
        if len(allottees) == 1:
            lines.append(f'🏢 배정대상: {allottees[0]}')
        else:
            lines.append('🏢 배정대상:\n' + '\n'.join(f'  • {a}' for a in allottees))

    return lines


def parse_contract(kv: dict) -> list:
    """단일판매ㆍ공급계약체결 / 수주"""
    lines = []

    # 계약명
    if v := _get(kv, '체결계약명', '계약명'):
        lines.append(f'📋 계약명: {_trunc(v, 50)}')

    # 계약상대 + 지역
    party  = _get(kv, '계약상대', '거래상대방', '발주처', '매수인')
    region = _get(kv, '판매ㆍ공급지역', '공급지역', '수주지역', '납품지역')
    if party:
        lines.append(f'🏢 상대방: {party}' + (f' ({region})' if region else ''))

    # 계약금액 + 매출비중
    amount = _get(kv, '계약금액(원)', '계약금액', '공급금액', '수주금액', '거래금액')
    ratio  = _get(kv, '매출액대비(%)', '최근매출액대비', '매출액 대비')
    if amount:
        ratio_str = f' (매출대비 {ratio}%)' if ratio else ''
        lines.append(f'💰 계약금액: {_fmt_amount(amount)}원{ratio_str}')

    # 계약기간
    start = _get(kv, '시작일')
    end   = _get(kv, '종료일')
    if start and end:
        lines.append(f'📅 계약기간: {start} ~ {end}')
    elif start:
        lines.append(f'📅 시작일: {start}')

    # 대금지급 조건
    if v := _get(kv, '대금지급 조건', '지급조건', '대금지급'):
        lines.append(f'💳 지급조건: {_trunc(v, 50)}')

    return lines


def _strip_disclaimer(text: str) -> str:
    """※ 투자유의사항 면책 문구 제거 (주요내용 앞부분).

    전략:
    1. '상존합니다' 뒤에 실제 내용이 있으면 그 이후만 반환
    2. 없으면 빈 문자열 반환 (제목으로 충분)
    """
    if not text.startswith('※'):
        return text

    # '상존합니다' 이후 실제 내용 추출
    m = re.search(r'상존합니다[.。]?\s*', text)
    if m:
        rest = text[m.end():].strip()
        # 2차 면책문구 제거: '투자자는 수시공시... 바랍니다.' 패턴
        rest = re.sub(r'^투자자는\s+수시공시.*?바랍니다[.。]?\s*', '', rest, flags=re.DOTALL).strip()
        return rest  # 빈 문자열이어도 OK (caller가 빈 경우 skip)

    # '상존합니다' 없어도 ※로 시작하면 전체가 면책 → 빈 문자열
    return ''


def parse_mgmt_event(kv: dict) -> list:
    """투자판단관련주요경영사항 — 임상·기술이전·계약 등"""
    lines = []

    # 자회사 여부
    subsidiary = _get(kv, '자회사인')
    if subsidiary:
        lines.append(f'🏢 자회사: {subsidiary}')

    # 제목 (1. 제목)
    if v := _get(kv, '1. 제목', '제목'):
        lines.append(f'📌 {_trunc(v, 80)}')

    # 주요내용 (면책 문구 제거 후 핵심만, 내용 없으면 생략)
    if v := _get(kv, '2. 주요내용', '주요내용', '결정내용'):
        stripped = _strip_disclaimer(v).strip()
        if stripped and len(stripped) > 5:   # 의미 있는 내용만
            lines.append(f'📋 {_trunc(stripped, 150)}')

    # 결정일 / 사실확인일
    if v := _get(kv, '이사회결의일', '사실확인일', '결정일'):
        lines.append(f'📅 결정일: {v}')

    # 관련공시 (이전 공시 참조)
    if v := _get(kv, '관련공시'):
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


def parse_trading_halt(kv: dict) -> list:
    """주권매매거래정지"""
    lines = []

    # 정지사유
    if v := _get(kv, '2.정지사유', '정지사유'):
        lines.append(f'⏸ 정지사유: {v}')

    # 정지일시: 날짜 형식 키(YYYY-MM-DD)에 시간 값이 붙어있음
    for k, v in kv.items():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', k) and v:
            lines.append(f'🕐 정지일시: {k} {v}')
            break

    # 만료
    if v := _get(kv, '나.만료일시', '만료일시', '재개일시'):
        lines.append(f'🔓 만료: {v}')

    return lines


def parse_amendment(kv: dict) -> list:
    """
    [기재정정] 공시 전용 파서 — 변경된 항목만 추출.

    DART 정정 공시 KV 구조:
      정정항목: 정정전 정정후  ← 헤더
      "N. 섹션 - 필드명": OLD  ← 변경된 필드
      OLD: NEW                 ← 3셀 행에서 old→new 매핑으로 저장됨
    """
    lines = []

    # ── 원공시 + 정정사유 ──────────────────────────────
    orig_doc  = _get(kv, '1. 정정관련 공시서류')
    orig_date = _get(kv, '2. 정정관련 공시서류제출일', '공시서류제출일')
    if orig_doc:
        lines.append(f'📄 {orig_doc}' + (f' ({orig_date})' if orig_date else ''))

    if v := _get(kv, '3. 정정사유', '정정사유'):
        lines.append(f'📋 사유: {_trunc(v, 80)}')

    # ── 변경 항목 추출 ────────────────────────────────
    # 정정항목 헤더 이후, "N. 섹션명 - 필드명: OLD" 패턴을 찾아
    # kv[OLD] = NEW 로 저장된 정정후 값과 연결
    items = list(kv.items())
    header_idx = next((i for i, (k, _) in enumerate(items) if k == '정정항목'), None)

    if header_idx is not None:
        i = header_idx + 1
        while i < len(items):
            k, old_val = items[i]
            # "N. 섹션명 - 필드명" 패턴: 정정 항목 행
            m = re.match(r'^\d+\.\s+.+\s+-\s+(.+)$', k)
            if not m:
                break  # 정정 섹션 끝
            field_name = m.group(1).strip()
            new_val    = kv.get(old_val.strip(), '')
            old_clean  = old_val.strip()
            new_clean  = new_val.strip()
            if old_clean and new_clean and old_clean != new_clean:
                lines.append(f'🔧 {field_name}')
                lines.append(f'   전: {_trunc(old_clean, 70)}')
                lines.append(f'   후: {_trunc(new_clean, 70)}')
            elif old_clean:
                lines.append(f'🔧 {field_name}: {_trunc(old_clean, 70)}')
            i += 2  # field→old 항목 + old→new 항목 2개씩 건너뜀

    return lines


# 공시 제목 키워드 → 카테고리 파서 매핑
# ※ 순서 중요: 구체적인 타입을 먼저, 일반적인 타입을 나중에
_PARSER_MAP = [
    (['유무상증자'],                         parse_combined_ci),
    (['유상증자'],                          parse_rights_offering),
    (['단일판매', '공급계약체결', '수주'],   parse_contract),
    (['투자판단관련주요경영사항'],           parse_mgmt_event),
    (['거래정지', '매매거래정지'],           parse_trading_halt),
    (['권리락'],                             parse_ex_rights),
    # 추후 추가: 무상증자, 전환사채, 합병, 잠정실적 등
]


# ══════════════════════════════════════════════
#  공개 인터페이스
# ══════════════════════════════════════════════

def get_disclosure_detail(rcept_no: str, report_nm: str) -> str:
    """
    공시 원문을 파싱하여 핵심 필드 텍스트 반환.
    파싱 실패 또는 필드 없으면 빈 문자열 반환 (graceful fallback).

    현재: 범용 파서(parse_all_fields)로 전체 필드 추출.
    추후: 카테고리별 파서로 포맷 고도화 예정.
    """
    html = _fetch_html(rcept_no)
    if not html:
        log.debug(f'[DART 파서] HTML 없음 ({rcept_no})')
        return ''

    try:
        kv = _build_kv(html)
        if not kv:
            log.debug(f'[DART 파서] KV 없음 ({report_nm})')
            return ''

        if report_nm.startswith('[기재정정]'):
            log.debug(f'[DART 파서] 기재정정 kv 키: {list(kv.keys())[:10]}')

        # 카테고리별 파서 시도 ([기재정정] 등 접두어 제거)
        clean_nm = re.sub(r'^\[[^\]]+\]', '', report_nm).strip()
        parser = None
        for keywords, fn in _PARSER_MAP:
            if any(k in clean_nm for k in keywords):
                parser = fn
                break

        is_amendment = report_nm.startswith('[기재정정]')

        # [기재정정]: 변경 항목만 보여주는 전용 파서 우선 사용
        if is_amendment:
            lines = parse_amendment(kv)
            if lines:
                lines.insert(0, '🔄 정정 내용')
                return '\n'.join(lines)

        if parser:
            lines = parser(kv)
            if lines:
                log.debug(f'[DART 파서] 카테고리 파서 사용 ({report_nm})')
                return '\n'.join(lines)

        # 범용 파서 fallback
        lines = parse_all_fields(kv)
        if not lines:
            log.debug(f'[DART 파서] 파싱 결과 없음 ({report_nm})')
        return '\n'.join(lines)

    except Exception as e:
        log.warning(f'[DART 파서] 파싱 실패 ({report_nm}): {e}')
        return ''
