"""
dart_doc.py — DART 공시 원문 취득·KV 추출 코어

dart_parser.py(파사드)에서 분리한 문서 계층:
  API 조회(list/majorstock) · 원문 fetch 3단 폴백(document.xml → zip.do →
  뷰어 스크래핑+UTF-8 손상 복구) · _build_kv 테이블 파싱 · _get/_trunc
  공용 유틸 · parse_all_fields 범용 파서
"""
import re
import logging
from bs4 import BeautifulSoup
from managers import global_session as _session

log = logging.getLogger(__name__)

_DART_API_BASE = 'https://opendart.fss.or.kr/api'


def _fetch_dart_list_item(rcept_no: str) -> dict:
    """
    DART OpenAPI list.json에서 rcept_no에 해당하는 공시 항목 반환.
    flr_nm(제출인명), corp_code 등 포함.
    """
    try:
        from config import DART_API_KEY
        if not DART_API_KEY:
            return {}
        date = rcept_no[:8]
        for page in range(1, 8):
            resp = _session.get(
                f'{_DART_API_BASE}/list.json',
                params={
                    'crtfc_key': DART_API_KEY,
                    'bgn_de': date,
                    'end_de': date,
                    'page_count': 100,
                    'page_no': page,
                },
                timeout=6,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            if data.get('status') != '000':
                break
            items = data.get('list', [])
            if not items:
                break
            for item in items:
                if item.get('rcept_no') == rcept_no:
                    return item
    except Exception as e:
        log.debug(f'[DART API] list 조회 실패 ({rcept_no}): {e}')
    return {}


def _fetch_dart_reporter(rcept_no: str) -> str:
    """list.json에서 flr_nm(공시제출인명) 반환. 하위호환용."""
    return (_fetch_dart_list_item(rcept_no).get('flr_nm') or '').strip()


def _fetch_dart_majorstock(rcept_no: str) -> dict:
    """
    DART majorstock.json으로 대량보유 상세 데이터 조회.
    반환 필드: repror, stkqy, stkrt, stkrt_irds, ctr_stkrt, report_resn 등.
    """
    try:
        from config import DART_API_KEY
        if not DART_API_KEY:
            return {}
        # corp_code 먼저 획득
        list_item = _fetch_dart_list_item(rcept_no)
        corp_code = list_item.get('corp_code', '')
        if not corp_code:
            return {}
        resp = _session.get(
            f'{_DART_API_BASE}/majorstock.json',
            params={'crtfc_key': DART_API_KEY, 'corp_code': corp_code},
            timeout=8,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        if data.get('status') != '000':
            return {}
        for item in data.get('list', []):
            if item.get('rcept_no') == rcept_no:
                return item
    except Exception as e:
        log.debug(f'[DART API] majorstock 조회 실패 ({rcept_no}): {e}')
    return {}


_DESKTOP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)


def _fetch_zip_original(rcept_no: str) -> str | None:
    """DART 웹사이트 '원본파일 다운로드'(zip.do)로 공시 원문 획득.

    document.xml API는 접수 당일 status 014(미제공)지만, 웹사이트 원본 zip은
    접수 직후에도 제공되며 인코딩이 깨끗함(실측). 브라우저 흐름 재현 필수:
    뷰어(main.do, dcmNo 추출) → 다운로드 팝업(쿠키/Referer) → zip.do.

    strict 디코딩(utf-8→cp949→euc-kr)이 성공할 때만 텍스트 반환.
    어떤 실패든 None → 호출측이 기존 뷰어 스크래핑으로 그대로 폴백.
    """
    import zipfile, io as _io
    base = 'http://dart.fss.or.kr'
    h = {'User-Agent': _DESKTOP_UA}
    try:
        viewer_url = f'{base}/dsaf001/main.do?rcpNo={rcept_no}'
        idx = _session.get(viewer_url, headers=h, timeout=8)
        if idx.status_code != 200:
            return None
        m = re.search(r'viewDoc\("(\d+)",\s*"(\d+)"',
                      idx.content.decode('utf-8', errors='replace'))
        if not m:
            return None
        rcp, dcm = m.group(1), m.group(2)

        popup_url = f'{base}/pdf/download/main.do?rcp_no={rcp}&dcm_no={dcm}'
        _session.get(popup_url, headers={**h, 'Referer': viewer_url}, timeout=8)

        zr = _session.get(
            f'{base}/pdf/download/zip.do?rcp_no={rcp}&dcm_no={dcm}',
            headers={**h, 'Referer': popup_url}, timeout=12)
        if zr.status_code != 200 or zr.content[:2] != b'PK':
            return None

        z = zipfile.ZipFile(_io.BytesIO(zr.content))
        # 본문 파일 선택: xml/html 우선, 그중 최대 크기
        names = [n for n in z.namelist()
                 if n.lower().endswith(('.xml', '.html', '.htm'))] or z.namelist()
        if not names:
            return None
        name = max(names, key=lambda n: z.getinfo(n).file_size)
        raw = z.read(name)
        if len(raw) < 100:
            return None
        # strict 디코딩 성공 시에만 채택 (손상 콘텐츠 반환 방지)
        for enc in ('utf-8', 'cp949', 'euc-kr'):
            try:
                text = raw.decode(enc)
                # DART 원본 XML 전용 개행 엔티티 제거 — &cr; 및 이중 이스케이프
                # &amp;cr; 형태 모두 (뷰어/API 경로엔 없음)
                return re.sub(r'&(?:amp;)?(?:cr|lf|tab);', ' ', text)
            except (UnicodeDecodeError, LookupError):
                continue
        return None
    except Exception as e:
        log.debug(f'[DART 파서] zip.do 원본 실패 ({rcept_no}): {e}')
        return None


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
    """
    DART 공시 본문 HTML 가져오기.
    1순위: DART OpenAPI document.xml (zip) — 인코딩 손상 없음.
           단, 접수 당일 공시는 status 014(파일 미존재) — 다음날에야 제공됨(실측).
    1.5순위: DART 웹사이트 원본파일 다운로드(zip.do) — 접수 직후에도 제공,
           인코딩 깨끗(실측: viewer.do가 깨뜨린 문서도 zip.do는 손상 0).
           strict 디코딩 성공 시에만 반환 → 실패 시 기존 2순위로 그대로 폴백.
    2순위: 웹 뷰어 스크래핑 + _fix_dart_utf8 복구 (특정 XML 문서 인코딩 손상 가능)
    """
    # ── 1순위: DART document.xml API ────────────────────────────────────────
    try:
        from config import DART_API_KEY
        if DART_API_KEY:
            import zipfile, io as _io
            api_resp = _session.get(
                f'{_DART_API_BASE}/document.xml',
                params={'crtfc_key': DART_API_KEY, 'rcept_no': rcept_no},
                timeout=12,
            )
            if api_resp.status_code == 200:
                try:
                    z = zipfile.ZipFile(_io.BytesIO(api_resp.content))
                    xml_bytes = z.read(z.namelist()[0])
                    decoded = xml_bytes.decode('utf-8', errors='replace')
                    log.debug(f'[DART 파서] document.xml API 사용 ({rcept_no})')
                    return decoded
                except Exception as e:
                    log.debug(f'[DART 파서] document.xml zip 실패 ({rcept_no}): {e}')
    except Exception as e:
        log.debug(f'[DART 파서] document.xml API 접근 실패: {e}')

    # ── 1.5순위: 웹사이트 원본파일 다운로드 (당일 공시 대응) ────────────────
    zip_text = _fetch_zip_original(rcept_no)
    if zip_text:
        log.debug(f'[DART 파서] zip.do 원본파일 사용 ({rcept_no})')
        return zip_text

    # ── 2순위: 웹 뷰어 스크래핑 ─────────────────────────────────────────────
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

    # 기재정정 공시에서 [항목 | 정정전 | 정정후] 3컬럼 테이블 헤더를 만나면 True
    _in_amendment_cols = False

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
                # [항목 | 정정전 | 정정후] 헤더 감지
                if '정정전' in cells and '정정후' in cells:
                    _in_amendment_cols = True
                elif _in_amendment_cols and cells[0] and (cells[1] or cells[2]):
                    # 정정전/후 데이터 행: 정정전_필드명 / 정정후_필드명 키로 저장
                    field = cells[0]
                    before = cells[1] if len(cells) > 1 else ''
                    after  = cells[2] if len(cells) > 2 else ''
                    if before:
                        kv[f'정정전_{field}'] = before
                    if after:
                        kv[f'정정후_{field}'] = after
                elif cells[0] and cells[1]:
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


def _trunc_clean(text: str, limit: int) -> str:
    """limit 초과 시 마지막 공백 경계에서 끊어 '…' 추가 (숫자·괄호 중간 절단 방지)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = cut.rfind(' ')
    if idx > limit * 0.5:
        cut = cut[:idx]
    return cut.rstrip(' ,(') + '…'


# ══════════════════════════════════════════════
#  범용 파서 — 전체 필드 추출
# ══════════════════════════════════════════════

# 출력 제외할 키 패턴 (서명·연락처·절차성 정보 등 노이즈)
_SKIP_KEY_PATTERNS = [
    '날인', '서명', '인(印)', '확인자', '위임장',
    '본점소재지', '법인등록번호', '사업자등록번호',
    '전화번호', '팩스번호', '홈페이지', 'E-mail',
    '작성책임자', '공시담당자', '담당부서', '대표이사',
    '주민등록번호', '주소',
    # 투자정보 가치 없는 절차성 항목 (2026-07-17 가독성 감사)
    '참석여부', '불참', '공정거래위원회', '공시유보', '유보사유', '대규모법인여부',
    # 기재정정 헤더 필드 — parse_amendment가 📄/📋로 이미 표시 (sub 출력 중복 방지)
    '정정관련', '정정사유', '정정일자',
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

# 값 끝 단위 라벨 — '차입금액(원)', '발행예정주식수(주)' 등 컬럼 헤더 판정용
_UNIT_PAREN = re.compile(r'\((원|주|%|명|건|회|백만원|천원|억원)\)$')

# 컬럼 헤더 어휘 — 모든 토큰이 이 어휘로만 구성된 값은 헤더로 판정
_HEADERISH = ('사업연도', '차입전', '차입후', '병합전', '병합후', '감자전', '감자후',
              '변경전', '변경후', '조정전', '조정후', '취득전', '취득후',
              '시작일', '종료일', '구분', '증감금액', '증감비율',
              '흑자적자전환여부', '당해', '직전', '발행일')


def _is_header_val(v: str) -> bool:
    """값이 데이터가 아니라 표의 컬럼 헤더로 보이는지 판정.

    다열 표에서 [헤더행|헤더행], [값|값] 쌍이 KV로 밀려 들어오면
    '1. 단기차입내역: 차입금액(원)', '결산기간: 당해사업연도 직전사업연도' 같은
    무의미 라인이 생긴다 — 숫자가 전혀 없는 짧은 라벨성 값만 보수적으로 거른다.
    """
    if len(v) > 30 or re.search(r'\d', v):
        return False
    if _UNIT_PAREN.search(v):
        return True
    if v.endswith('여부'):    # 값이 '~여부'면 헤더 ('미해당'/'예' 등이 실제 값)
        return True
    toks = [t for t in v.split() if t not in ('-', '–', '—')]   # 빈칸 대시 무시
    return bool(toks) and all(any(h in t for h in _HEADERISH) for t in toks)


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
    # 정규화 키 집합 — "값이 다른 행의 키와 동일" = 컬럼 헤더 짝밀림 판정용
    key_set = {re.sub(r'\s+', ' ', k).strip() for k in kv if not k.startswith('_')}

    for k, v in kv.items():
        # 내부 키(_html·_rcept_no 등) 노출 방지
        if k.startswith('_'):
            continue
        k = re.sub(r'\s+', ' ', k).strip()
        v = re.sub(r'\s+', ' ', v).strip()

        # 빈 값 / 의미없는 값 / 대시뿐인 값('- -') 제외
        if not v or v in _SKIP_VALUES or re.fullmatch(r'[\s\-—–~.]+', v):
            continue
        # 키가 너무 짧거나 없으면 제외
        if not k or len(k) < 2:
            continue
        # 노이즈 키 제외 (공백 변형 '참석 여부' 대응 — 공백 제거 후 매칭)
        k_flat = re.sub(r'\s+', '', k)
        if any(p in k_flat for p in _SKIP_KEY_PATTERNS):
            continue
        # 기재정정 원시 키 제외 — 변경 비교는 parse_amendment(🔧)가 담당,
        # 원시 쌍은 짝이 어긋난 채 노출되므로 범용 출력에서 제거
        if k.startswith(('정정전', '정정후')):
            continue
        # 표 짝밀림 행 제외 ①: 숫자·날짜 데이터가 키 자리로 밀린 경우
        # ('65,000,000,000: 85,000,000,000', '2026-01-01: 2025-07-01' 등)
        if re.match(r'^[\d,.\s\-/%()~:]+$', k):
            continue
        # 표 짝밀림 행 제외 ②: 값이 컬럼 헤더인 경우 ('결산기간: 당해사업연도 직전사업연도')
        if _is_header_val(v):
            continue
        # 표 짝밀림 행 제외 ③: 순한글 라벨값이 다른 행의 키와 동일
        # ('6. 합병상대회사: 회사명' — 실데이터는 '회사명: …' 행에 별도 존재)
        # ※ 순한글 한정: 영문 회사명 등 실데이터가 짝밀림으로 키에도 들어간 경우
        #    실데이터 행까지 지우는 오삭제 방지
        if len(v) <= 20 and v in key_set and re.fullmatch(r'[가-힣\s()·ㆍ]+', v):
            continue
        # 종속회사 서식 조각 ('(회사명): 의 주요경영사항 신고')
        if re.match(r'^의\s*주요경영사항', v):
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
