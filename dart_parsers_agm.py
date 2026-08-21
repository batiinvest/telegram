"""
dart_parsers_agm.py — 주총·임원·경영·IR 관련 공시 파서 (경영권·사외이사·임원변경·주총·본사이전·IR)
(2026-07 dart_parsers_governance 세분화 — 파서 원문 무변경 이식)
"""
import re  # noqa: F401
from dart_parse_helpers import *  # noqa: F401,F403  헬퍼·상수·_get/_trunc·log


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
        if lines:            # 시험 개요 ↔ 결과 블록 구분 (앞 내용 있을 때만 빈 줄)
            lines.append('')
        sec_lines = _parse_clinical_result(result_val)
        if sec_lines:
            lines.append('🔬 결과:')
            lines.extend(sec_lines)
        else:
            # 섹션 헤더 없어도 '- ' 불릿이 여럿이면 줄바꿈 (단순 나열 가독성)
            _rv = re.sub(r'\s+', ' ', result_val).strip()
            _bul = [re.sub(r'^-\s*', '', b.strip())
                    for b in re.split(r'\s+-\s+', _rv) if b.strip()]
            if len(_bul) >= 2:
                lines.append('🔬 결과:')
                for b in _bul[:6]:
                    lines.append(f'  • {_trunc_clean(b, 300)}')
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
        lines.append(f'🔗 관련: {_trunc(v, 110)}')

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
        lines.append(f'🔗 관련: {_trunc(v, 110)}')

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
    _f(lines, kv, '🔗 관련', '관련공시', '※관련공시', trunc=110)

    # KV 테이블에서 못 뽑음(소집공고 자유서식) → 본문 텍스트 파싱 폴백
    if not lines:
        return _parse_agm_notice_text(kv)

    return lines
