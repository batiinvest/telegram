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
    0xEB~0xED 리드 바이트가 0x3F(?)로 손상된 3바이트 UTF-8 시퀀스를 복원.

    DART 손상 패턴:
    - 3F+cont1+cont2: 리드 바이트만 3F로 치환된 경우 (복구 가능)
    - 3F+3F: EUC-KR 2바이트를 각 바이트별로 3F로 치환 (복구 불가)

    cont1 범위별 유효 리드 바이트:
    - 0x80–0x9E: EB/EC/ED 모두 한글 가능
    - 0x9F:      EB/EC (ED→U+D7C0+ 한글 범위 초과)
    - 0xA0–0xAF: EB/EC (EA→U+A800 이하, ED→U+D800+ 모두 한글 아님)
    - 0xB0–0xBF: EA/EB/EC (ED→U+D800+ 초과)

    우선순위 결정: 자주 등장하는 글자 집합으로 리드 바이트 추정.
    """
    # 임원/기업 문서에 자주 등장하는 ED 범위 한글 (U+D000–D7A3)
    _ED_PREF = frozenset(
        '현회해화호한히협허형행항혜힘특트터탈택탄태통투티팀'
        '포표편폐평필피하학할핵험혁헌헤헬확환활효후훈휴흔희'
        '턱턴털텀텅텐텔템토톤톨톱퇴툭툴툼튀튜틱틸틈틀'
    )
    # 자주 등장하는 EB 범위 한글 (U+B000–BFFF)
    _EB_PREF = frozenset(
        '나남내너네노농누능니다달담당대더데도독동두드디'
        '라락란랄람랍랑래랙랜랭량러럭런렁렇렉렌렐렙롯롱루룹릭릴리'
        '마막만말맘맙망매맥맨맹머먹먼멀멈멥명모목몬몰몸몹못몽무묵문물뭄므미'
        '바박반발밤밥방배백밴밸버벅번벌범법별병보복본볼봄봇봉부북분불뷰브비빅빈빌빔빗빙'
    )

    def _best_lead(cont1: int, cont2: int) -> int | None:
        """cont1/cont2로 최적 리드 바이트 결정."""
        # 후보 리드 바이트 결정
        if cont1 >= 0xB0:
            candidates = (0xEA, 0xEB, 0xEC)
        elif cont1 >= 0x9F:
            candidates = (0xEB, 0xEC)
        else:  # 0x80–0x9E
            candidates = (0xEB, 0xEC, 0xED)

        valids = []
        for lead in candidates:
            try:
                ch = bytes([lead, cont1, cont2]).decode('utf-8')
                if '가' <= ch <= '힣':
                    valids.append((lead, ch))
            except (UnicodeDecodeError, ValueError):
                pass

        if not valids:
            return None
        if len(valids) == 1:
            return valids[0][0]

        # 복수 후보: 선호 집합 → EC 기본 순으로 우선순위
        for lead, ch in valids:
            if lead == 0xED and ch in _ED_PREF:
                return 0xED
        for lead, ch in valids:
            if lead == 0xEB and ch in _EB_PREF:
                return 0xEB
        # 기본: EC (가장 넓은 한글 커버, DART 원래 버그 대상)
        for lead, _ in valids:
            if lead == 0xEC:
                return 0xEC
        return valids[0][0]

    buf = bytearray()
    i   = 0
    n   = len(content)
    while i < n:
        b = content[i]
        if (b == 0x3F
                and i + 2 < n
                and 0x80 <= content[i+1] <= 0xBF
                and 0x80 <= content[i+2] <= 0xBF):
            lead = _best_lead(content[i+1], content[i+2])
            if lead is not None:
                buf.extend(bytes([lead, content[i+1], content[i+2]]))
                i += 3
            else:
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


def _clean_party(raw: str) -> str:
    """계약상대방 값에서 참고사항 주석(- 상기...) 제거 후 업체명만 반환.
    값이 '- 상기...' 형태로 시작하면 내부 고유명사(대문자+괄호 패턴) 추출 시도."""
    if not raw:
        return raw
    # 줄 단위로 분리 후 주석 전 첫 번째 실제 값 추출
    first_line = raw.split(' - ')[0].strip()
    if first_line and not first_line.startswith('-'):
        return first_line[:80]
    # 값 전체가 주석으로 시작하는 경우 — 영문 업체명 패턴 추출 시도
    m = re.search(r'([A-Z][A-Za-z0-9\s\(\)]+(?:Co\.|Corp\.|Ltd\.|LLC|Inc\.|Board|Project|Power|Plant|Vietnam|Korea|China|Japan|USA)[A-Za-z0-9\s\(\)]*)', raw)
    if m:
        return m.group(1).strip()[:80]
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

    # 계약상대 + 지역 — 참고사항 주석 제거
    party  = _get(kv, '계약상대', '거래상대방', '발주처', '매수인')
    region = _get(kv, '판매ㆍ공급지역', '공급지역', '수주지역', '납품지역')
    if party:
        party_clean = _clean_party(party)
        lines.append(f'🏢 상대방: {party_clean}' + (f' ({_trunc(region, 30)})' if region else ''))

    # 계약금액 + 매출비중 — 정정전/후 두 값 처리
    amount = _get(kv, '계약금액(원)', '계약금액', '공급금액', '수주금액', '거래금액')
    ratio  = _get(kv, '매출액대비(%)', '최근매출액대비', '매출액 대비')
    if amount:
        ratio_str = f' (매출대비 {_clean_ratio(ratio)})' if ratio else ''
        lines.append(f'💰 계약금액: {_fmt_amount(amount)}원{ratio_str}')

    # 계약기간 — 날짜만 추출, 참고사항 제거
    start = _get(kv, '시작일')
    end   = _get(kv, '종료일')
    start_clean = _clean_date(start) if start else None
    end_clean   = _clean_date(end)   if end   else None
    if start_clean and end_clean:
        lines.append(f'📅 계약기간: {start_clean} ~ {end_clean}')
    elif start_clean:
        lines.append(f'📅 시작일: {start_clean}')

    # 대금지급 조건
    if v := _get(kv, '대금지급 조건', '지급조건', '대금지급'):
        lines.append(f'💳 지급조건: {_trunc(v, 60)}')

    return lines


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


def _parse_numbered_body(text: str, max_items: int = 7) -> list[str]:
    """
    '1) 항목명 - 내용' 또는 '1. 항목명: 내용' 형태 번호 목록을 줄별 bullet로 변환.
    """
    # 번호 목록 분리: '1)' 또는 '1. ' 패턴 모두 지원
    parts = re.split(r'\s*(?<!\w)(\d{1,2})[.)]\s+', text)
    # parts = ['prefix', '1', 'content1 ', '2', 'content2 ', ...]
    items = []
    i = 1
    while i < len(parts) - 1:
        content = parts[i + 1].strip()
        # 'key: value' 또는 'key - value' 분리
        m = re.match(r'^(.{1,25}?)\s*[:－-]\s*(.+)', content, re.DOTALL)
        if m:
            key = m.group(1).strip()
            val = re.sub(r'\s+', ' ', m.group(2)).strip()
            # 핵심값 앞부분 추출 (dash 이후 부연설명 제거하고 80자)
            val_short = _trunc(val.split(' - ')[0].strip() if ' - ' in val else val, 80)
            # 너무 짧은 값(헤더성)은 생략
            if len(val_short) < 3 or val_short in ('없음', '-', '해당없음'):
                i += 2
                continue
            items.append(f'  • {key}: {val_short}')
        else:
            short = re.sub(r'\s+', ' ', content).strip()
            # 단순 섹션 헤더(짧고 콜론/값 없는 것)는 생략
            if 10 <= len(short) <= 80:
                items.append(f'  • {short}')
        i += 2
        if len(items) >= max_items:
            break
    return items


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
            # 앞의 '- ' 또는 '· ' 제거 후 문장 단위로 자연스럽게 truncate
            clean = re.sub(r'^[\-·•]\s*', '', stripped).strip()
            # 150자 내에서 마지막 문장 종결('. ') 위치를 찾아 그 앞까지 표시
            if len(clean) > 150:
                cut = clean[:150]
                # 마지막 '. ' 위치에서 끊기
                last_period = cut.rfind('. ')
                if last_period > 60:
                    cut = cut[:last_period + 1]
                else:
                    # 마지막 공백에서 끊기
                    last_space = cut.rfind(' ')
                    if last_space > 60:
                        cut = cut[:last_space]
                clean = cut + '…'
            lines.append(f'  {clean}')

    # 시험결과 (임상시험결과 공시)
    result_val = _get(kv, '2) 결과값', '결과값')
    if result_val:
        lines.append(f'🔬 결과: {_trunc(result_val, 150)}')

    # 변경신청 사유 (변경승인 공시)
    if v := _get(kv, '3. 변경신청 사유', '변경신청 사유', '변경사유'):
        lines.append(f'📋 변경사유: {_trunc(v, 70)}')

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

    # 발행금액
    if v := _get(kv, '2. Total face', 'Total face (or electronically registered) value'):
        lines.append(f'💰 발행금액: {_fmt_amount(v)}원')

    # 전환가액
    if v := _get(kv, 'Conversion price (KRW/share)', 'Exercise price'):
        lines.append(f'💵 전환가액: {v}원/주')

    # 이자율 / 만기수익률
    coupon = _get(kv, 'Coupon rate', '4. Interest rate of bonds')
    ytm    = _get(kv, 'Yield to maturity')
    if coupon:
        ytm_str = f' / YTM {ytm}%' if ytm and ytm != coupon else ''
        lines.append(f'📊 이자율: {coupon}%{ytm_str}')

    # 만기
    if v := _get(kv, '5. Bond maturity date', 'Maturity date'):
        lines.append(f'📅 만기: {v}')

    # 전환청구기간
    start = _get(kv, 'Start date')
    end   = _get(kv, 'End date')
    if start and end:
        lines.append(f'📅 전환청구: {start} ~ {end}')

    # 발행방식
    if v := _get(kv, '8. Method of bond issuance'):
        lines.append(f'📋 발행방식: {_BOND_METHOD.get(v, v)}')

    # 납입일
    if v := _get(kv, '12. Payment date', 'Payment date'):
        lines.append(f'📅 납입일: {v}')

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
        lines.append(f'⏸ 정지사유: {v}')

    for k, v in kv.items():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', k) and v:
            lines.append(f'🕐 정지일시: {k} {v}')
            break

    if v := _get(kv, '나.만료일시', '만료일시', '재개일시'):
        lines.append(f'🔓 만료: {v}')

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

    # 채권자
    if v := _get(kv, '2. 채권자', '채권자'):
        lines.append(f'🏦 채권자: {v}')

    # 차입금액
    if v := _get(kv, '3. 채무(차입)금액(원)', '채무(차입)금액'):
        lines.append(f'💳 차입금액: {_fmt_amount(v)}원')

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

    # 이사회결의일
    if v := _get(kv, '6. 이사회결의일(결정일)', '이사회결의일'):
        lines.append(f'📋 결의일: {v}')

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

    if v := _get(kv, '1. Contract amount (KRW)', 'Contract amount'):
        lines.append(f'💰 계약금액: {_fmt_amount(v)}원')

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

    if v := _get(kv, '5. Scheduled termination date', 'Scheduled termination date'):
        lines.append(f'📅 해지예정일: {v}')

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

    # 취득금액
    if v := _get(kv, '1. Contract amount (KRW)', 'Contract amount'):
        lines.append(f'💰 취득금액: {_fmt_amount(v)}원')

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

    # 결의일
    if v := _get(kv, '7. Board resolution date', 'Board resolution date'):
        lines.append(f'📋 결의일: {v}')

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
            f = float(v)
            sign = '+' if f > 0 else ''
            return f'{sign}{v}%'
        return ''

    for idx, (k, v) in enumerate(items):
        if k not in _METRICS:
            continue
        # 다음 4개 items에서 값 추출
        if idx + 3 >= len(items):
            continue
        _, curr_val = items[idx + 1] if idx + 1 < len(items) else (None, None)
        prev_val, _ = items[idx + 1] if False else (None, None)

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

    # 결정일
    if v := _get(kv, '4. 결정일자', '결정일자'):
        lines.append(f'📅 결정일: {v}')

    # 관련공시
    if v := _get(kv, '※ 관련공시', '관련공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

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

    # 변경일
    if v := _get(kv, '1. Date of change outside director', '1. Date of change in outside director'):
        lines.append(f'📅 변경일: {v}')

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

    if v := _get(kv, '2. 변경사유', '변경사유'):
        lines.append(f'📋 사유: {_trunc(v, 60)}')

    if v := _get(kv, '3. 이전(예정)일', '이전(예정)일', '이전일'):
        lines.append(f'📅 이전일: {v}')

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

    if v := _get(kv, '2. 장소', '장소'):
        lines.append(f'📍 장소: {_trunc(v, 40)}')

    if v := _get(kv, '3. 대상자', '대상자'):
        lines.append(f'👥 대상: {v}')

    if v := _get(kv, '4. 실시목적', '실시목적'):
        lines.append(f'📋 목적: {_trunc(v, 50)}')

    if v := _get(kv, '6. 주요내용', '주요내용'):
        lines.append(f'📋 내용: {_trunc(v, 50)}')

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

    # 취득 후 지분비율
    if v := _get(kv, '지분비율(%)'):
        lines.append(f'📊 취득 후 지분: {v}%')

    # 취득방법
    if v := _get(kv, '4. 취득방법', '취득방법'):
        lines.append(f'📋 취득방법: {_trunc(v, 60)}')

    # 취득목적
    if v := _get(kv, '5. 취득목적', '취득목적'):
        lines.append(f'📋 목적: {_trunc(v, 60)}')

    # 취득예정일자
    if v := _get(kv, '6. 취득예정일자', '취득예정일자'):
        lines.append(f'📅 취득예정: {v}')

    # 관련공시
    if v := _get(kv, '※ 관련공시', '관련공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

    return lines


def parse_agm_notice(kv: dict) -> list:
    """주주총회소집결의 / 소집공고"""
    lines = []

    date = _get(kv, '날짜', 'Date')
    time = _get(kv, '시간', 'Time')
    if date:
        lines.append(f'📅 일시: {date}' + (f' {time}' if time else ''))

    if v := _get(kv, '2. 장소', '장소', 'Place'):
        lines.append(f'📍 장소: {_trunc(v, 50)}')

    if v := _get(kv, '-주주총회 구분', '주주총회 구분'):
        lines.append(f'📋 구분: {v}')

    if v := _get(kv, '3. 의결권행사기준일', '의결권행사기준일'):
        lines.append(f'📋 의결권기준일: {v}')

    if v := _get(kv, '관련공시', '※관련공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

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


def parse_major_shareholder_change(kv: dict) -> list:
    """최대주주변경"""
    lines = []

    # 변경전/후 최대주주 — 값이 '변경전 최대주주' / '변경후 최대주주'인 KV 탐색
    items = list(kv.items())
    before_name = before_shares = before_ratio = ''
    after_name  = after_shares  = after_ratio  = ''

    for idx, (k, v) in enumerate(items):
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
    """임원ㆍ주요주주 소유상황보고서"""
    lines = []

    # 보고자 + 직책
    reporter = _get(kv, '보고자', 'Name (Title)', 'Korean')
    position = _get(kv, 'Position')
    if reporter:
        lines.append(f'👤 보고자: {reporter}' + (f' ({position})' if position else ''))

    # 증감 + 비율
    change_str = _get(kv, 'Increase or decrease')
    total_issued = _get(kv, 'Total number of shares issued')
    current_str  = kv.get('Total')     # 현재 총 보유수 (정확한 키 매칭, substring 방지)

    if change_str:
        try:
            n = int(change_str.replace(',', ''))
            sign = '+' if n >= 0 else ''
            # 발행주식 대비 비율
            ratio_str = ''
            if total_issued:
                try:
                    t = int(total_issued.replace(',', ''))
                    if t > 0:
                        ratio = abs(n) / t * 100
                        ratio_sign = '+' if n >= 0 else '-'
                        ratio_str = f' ({ratio_sign}{ratio:.2f}%)'
                except (ValueError, AttributeError):
                    pass
            lines.append(f'📊 증감: {sign}{n:,}주{ratio_str}')
        except (ValueError, AttributeError):
            lines.append(f'📊 증감: {change_str}주')

    # 보고 전→후 보유 수량 변화
    if current_str and change_str:
        try:
            curr  = int(current_str.replace(',', ''))
            chng  = int(change_str.replace(',', ''))
            chng_abs = abs(chng)
            prev  = curr - chng   # 보고 전 = 현재 - 증감

            def _ratio(n: int) -> str:
                if not total_issued:
                    return ''
                try:
                    t = int(total_issued.replace(',', ''))
                    return f' ({n/t*100:.2f}%)' if t > 0 else ''
                except (ValueError, AttributeError):
                    return ''

            # 신규 취득(보고 전 0)이 아닌 경우만 전→후 표시
            if curr != chng_abs or chng < 0:
                arrow = '🔻' if chng < 0 else '🔺'
                lines.append(
                    f'📦 보유변화: {prev:,}주{_ratio(prev)} {arrow} {curr:,}주{_ratio(curr)}'
                )
            else:
                # 신규 취득
                lines.append(f'📦 현재보유: {curr:,}주{_ratio(curr)}')
        except (ValueError, AttributeError):
            pass

    # 발생일
    if v := _get(kv, 'Date of occurrence of reporting obligation', 'Date of occurrence'):
        lines.append(f'📅 발생일: {v}')

    return lines


def _fmt_amendment_val(field_name: str, val: str) -> str:
    """기재정정 비교값 포맷 — 금액/날짜/비율 필드에 맞게 변환."""
    if not val or val in ('-', '—', '없음', 'N/A'):
        return val
    # 금액 필드
    if any(kw in field_name for kw in ('금액', '가격', '대금', '보증금')):
        try:
            return _fmt_amount(val) + '원'
        except Exception:
            pass
    # 날짜 필드
    if any(kw in field_name for kw in ('일', '기간', '시작', '종료')):
        cleaned = _clean_date(val)
        if cleaned != val:
            return cleaned
    # 비율 필드
    if any(kw in field_name for kw in ('대비', '비율', '%', '비중')):
        nums = re.findall(r'\d+(?:\.\d+)?', val)
        if nums:
            return nums[0] + '%'
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
        lines.append(f'📋 사유: {_trunc(v, 80)}')

    change_lines = []

    # ── 패턴 C: 정정전_* / 정정후_* 접두어 키 비교 (가장 신뢰도 높음) ──────
    before_keys = {k[4:]: v for k, v in kv.items() if k.startswith('정정전')}
    after_keys  = {k[4:]: v for k, v in kv.items() if k.startswith('정정후')}
    for field, old_v in before_keys.items():
        new_v = after_keys.get(field, '')
        old_c = re.sub(r'\s+', ' ', old_v).strip()
        new_c = re.sub(r'\s+', ' ', new_v).strip()
        if old_c and new_c and old_c != new_c:
            old_fmt = _fmt_amendment_val(field, old_c)
            new_fmt = _fmt_amendment_val(field, new_c)
            change_lines.append(f'🔧 {field}: {old_fmt} → {new_fmt}')

    if change_lines:
        lines.extend(change_lines)
        return lines

    # ── 패턴 A / B: 정정항목 섹션 파싱 ──────────────────────────────────────
    items = list(kv.items())
    header_idx = next((i for i, (k, _) in enumerate(items) if k == '정정항목'), None)
    if header_idx is None:
        return lines

    i = header_idx + 1
    while i < len(items):
        k, val = items[i]

        # 패턴 A: "N. 섹션명 - 필드명": OLD  +  OLD: NEW
        m = re.match(r'^\d+\.\s+.+\s+-\s+(.+)$', k)
        if m:
            field_name = m.group(1).strip()
            new_val    = kv.get(val.strip(), '')
            old_clean  = val.strip()
            new_clean  = new_val.strip()
            if old_clean and new_clean and old_clean != new_clean:
                old_fmt = _fmt_amendment_val(field_name, old_clean)
                new_fmt = _fmt_amendment_val(field_name, new_clean)
                change_lines.append(f'🔧 {field_name}: {old_fmt} → {new_fmt}')
            elif old_clean:
                change_lines.append(f'🔧 {field_name}: {_fmt_amendment_val(field_name, old_clean)}')
            i += 2
            continue

        # 패턴 B: "N. 섹션명" 부모 헤더 → 하위 "- 필드: old" / "- 필드: new"
        if re.match(r'^\d+\.\s+\S', k):
            j = i + 1
            while j < len(items):
                ck, cv = items[j]
                if not ck.startswith('-'):
                    break
                mo = re.match(r'^-\s*(.+?):\s*(.+)$', ck)
                mn = re.match(r'^-\s*(.+?):\s*(.+)$', cv)
                if mo and mn:
                    fname  = mo.group(1).strip()
                    old_v  = mo.group(2).strip()
                    new_v  = mn.group(2).strip()
                    if old_v != new_v:
                        old_fmt = _fmt_amendment_val(fname, old_v)
                        new_fmt = _fmt_amendment_val(fname, new_v)
                        change_lines.append(f'🔧 {fname}: {old_fmt} → {new_fmt}')
                j += 1
            i = j
            continue

        break  # 정정 섹션 끝

    lines.extend(change_lines)
    return lines


# 공시 제목 키워드 → 카테고리 파서 매핑
# ※ 순서 중요: 구체적인 타입을 먼저, 일반적인 타입을 나중에
_PARSER_MAP = [
    (['유무상증자'],                         parse_combined_ci),
    (['유상증자'],                          parse_rights_offering),
    (['단일판매', '공급계약체결', '수주'],   parse_contract),
    (['전환사채', '신주인수권부사채'],        parse_cb),
    (['투자판단관련주요경영사항'],           parse_mgmt_event),
    (['임원ㆍ주요주주', '임원·주요주주'],     parse_insider_report),
    (['거래정지', '매매거래정지'],           parse_trading_halt),
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
]

# 상세 파싱 불필요 공시 유형 — 헤더만 표시 (parse_all_fields fallback 방지)
_SKIP_DETAIL_TYPES = frozenset([
    '대규모기업집단현황', '기업지배구조보고서',
    # 정기보고서류 — 수천 개 KV + 인코딩 깨짐, 헤더만 표시
    '사업보고서', '반기보고서', '분기보고서', '감사보고서',
])


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
        kv['_html'] = html  # 일부 파서에서 원문 직접 파싱 용도

        if report_nm.startswith('[기재정정]'):
            log.debug(f'[DART 파서] 기재정정 kv 키: {list(kv.keys())[:10]}')

        # 카테고리별 파서 시도 ([기재정정] 등 접두어 제거)
        clean_nm = re.sub(r'^\[[^\]]+\]', '', report_nm).strip()

        # 상세 불필요 공시 — 빈 문자열 즉시 반환
        if any(skip in clean_nm for skip in _SKIP_DETAIL_TYPES):
            return ''

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
                # 원 공시 핵심 내용도 추가 (카테고리 파서 적용 가능한 경우)
                if parser:
                    sub = parser(kv)
                    if sub:
                        lines.append('─' * 12)
                        lines.extend(sub)
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
