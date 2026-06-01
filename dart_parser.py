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
        # BeautifulSoup이 처리 못한 잔여 엔티티 제거 (&cr; &wbr; 등)
        t = re.sub(r'&[a-zA-Z]{1,8};', ' ', t)
        return re.sub(r'\s+', ' ', t).strip()

    for row in soup.find_all('tr'):
        cells = [_cell_text(c) for c in row.find_all(['td', 'th'])]
        cells = [c for c in cells if c]  # 빈 셀 제거

        n = len(cells)
        if n == 2:
            if cells[0] and cells[1]:
                kv[cells[0]] = cells[1]
        elif n == 3:
            if cells[0] and cells[1]:
                # 세 번째 셀이 단위(짧음)면 값에 붙이기, 아니면 별도 KV
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
            # 짝수 쌍으로 파싱
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
#  카테고리별 파서 (추후 분류별로 양식 정의 예정)
# ══════════════════════════════════════════════
#
#  현재는 parse_all_fields 가 기본으로 동작.
#  아래 파서들은 향후 카테고리별 포맷 정의 시 활성화.
#
# _CATEGORY_PARSERS = {
#     '유상증자':  parse_rights_offering,
#     '무상증자':  parse_bonus_issue,
#     '전환사채':  parse_cb_bw,
#     ...
# }


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

        is_amendment = report_nm.startswith('[기재정정]')
        if is_amendment:
            log.debug(f'[DART 파서] 기재정정 kv 키: {list(kv.keys())[:10]}')

        lines = parse_all_fields(kv)
        if not lines:
            log.debug(f'[DART 파서] 파싱 결과 없음 ({report_nm}) — kv 키: {list(kv.keys())[:5]}')
        return '\n'.join(lines)

    except Exception as e:
        log.warning(f'[DART 파서] 파싱 실패 ({report_nm}): {e}')
        return ''
