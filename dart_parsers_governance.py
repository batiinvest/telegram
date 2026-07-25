"""
dart_parsers_governance.py — 지배구조·지분·주총 관련 공시 파서 (경영권·임원·주총·대량보유·공개매수)
(2026-07 dart_parsers 분할 — 파서 원문 무변경 이식)
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


def parse_tender_offer(kv: dict) -> list:
    """공개매수신고서/공고 — 매수자·대상·목적·가격·수량·기간·주관사.

    구(舊): 전용 파서 없어 parse_all_fields 폴백 → '금융위원회 귀중'·전자공시 URL·
    체크박스(■□) 등 노이즈 벽. 핵심 필드는 표준 라벨로 깔끔히 키 접근됨.
    특히 목적의 ■ 체크 항목(상장폐지 등)이 투자 판단의 핵심."""
    lines = ['📢 공개매수']

    # 설명서는 상단이 '보 고 자', 신고서는 '공 개 매 수 자' — 둘 다 대응.
    # 값 형태: '성 명: SK…' 또는 'ㆍ성명 : SK… ■ 회사 □…'
    buyer = _get(kv, '공 개 매 수 자', '보 고 자', '공개매수자')
    if buyer:
        buyer = re.sub(r'^[ㆍ·\s]*성\s*명\s*[:：]\s*', '', buyer)
        buyer = re.split(r'\s*[■□]', buyer)[0].strip()   # 체크박스 이후 절단
        lines.append(f'🏢 매수자: {_trunc(buyer, 30)}')

    if v := _get(kv, '공개매수 대상회사명', '대상회사명'):
        lines.append(f'🎯 대상: {_trunc(v, 30)}')

    # 목적 — ■ 체크된 항목만 추출 (상장폐지·경영권안정·M&A·지주회사요건충족 등)
    if purpose := _get(kv, '공개매수 목적', '공개매수목적'):
        checked = re.findall(r'■\s*([가-힣A-Za-z&]+)', purpose)
        if checked:
            lines.append(f'📋 목적: {" · ".join(checked)}')

    if price := _get(kv, '매수 가격', '매수가격'):
        m = re.search(r'([\d,]+)\s*원', price)
        if m:
            lines.append(f'💵 매수가격: {m.group(1)}원')

    if qty := _get(kv, '매수 예정 수량(비율)', '매수 예정 수량', '매수예정수량'):
        ms = re.search(r'([\d,]{4,})\s*주', qty)
        mr = re.search(r'([\d.]+)\s*%', qty)
        if ms:
            rr = f' ({mr.group(1)}%)' if mr else ''
            lines.append(f'🔢 매수예정: {ms.group(1)}주{rr}')

    if period := _get(kv, '공개매수기간'):
        period = re.sub(r'\s+', ' ', period)
        lines.append(f'📅 기간: {_trunc(period, 55)}')

    agent = _get(kv, '사무취급자') or _get(kv, '대 리 인')
    if agent:
        agent = re.sub(r'^성\s*명\s*[:：]\s*', '', agent)
        lines.append(f'🏦 주관사: {_trunc(agent, 30)}')

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
    # 다중섹션 폼('담보권 전부 실행시')에서는 standalone 지분율이 잔여지분 비율과
    # 뒤섞여 오표기됨(현재 보유주식수에 실행후 지분율이 붙어 최대주주가 0.13%인 듯).
    # → 이 경우 지분율 대신 담보 실행시 잔여주식을 표시(최대주주변경 리스크가 핵심).
    shares   = _get(kv, '소유 주식 수(주)', '소유주식수(주)')
    exec_raw = _get(kv, '담보권 전부 실행시')
    exec_m   = re.search(r'[\d,]{3,}', exec_raw) if exec_raw else None
    if shares:
        if exec_m:
            lines.append(f'📊 보유주식: {int(shares.replace(",", "")):,}주'
                         f' → 담보실행시 {int(exec_m.group(0).replace(",", "")):,}주')
        else:
            ratio = _get(kv, '지분율(%)', '지분율')
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

    # 담보제공기간 — '시작일'/'종료일' 키가 헤더 텍스트를 값으로 갖는 경우가 있어
    # (다중담보 표: '시작일' => '종료일') 날짜 패턴 값만 채택. 실패 시 KV의
    # 날짜쌍 행('2026-07-15' => '2027-07-15')에서 첫 쌍 폴백.
    _DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
    start = _get(kv, '시작일', '담보제공기간시작일')
    end   = _get(kv, '종료일', '담보제공기간종료일')
    start = start if start and _DATE.search(start) else None
    end   = end   if end   and _DATE.search(end)   else None
    if not (start and end):
        for k, v in kv.items():
            if _DATE.fullmatch(k.strip()) and v and _DATE.fullmatch(v.strip()):
                start, end = start or k.strip(), v.strip()
                break
    if start and end:
        lines.append(f'📅 담보기간: {start} ~ {end}')
    elif start or end:
        lines.append(f'📅 담보기간: {start or end}')

    # 계약 체결일
    if v := _get(kv, '5. 담보권 설정계약 체결일(당해 건)', '담보권 설정계약 체결일'):
        lines.append(f'📝 계약체결일: {v}')

    return lines


def parse_share_transfer(kv: dict) -> list:
    """(최대주주변경을수반하는) 주식양수도계약체결 — 양도/양수인·거래규모·변경후 지분.

    구(舊): parse_major_shareholder_change로 새어 양도인이 뒤섞여 오출력됐음
    (양수도 서식엔 '변경전/후 최대주주' 값 표가 없어 오매칭). 핵심 필드는 깔끔히
    키로 접근됨 — 변경예정 최대주주(=양수인)·양수도 주식수/가액/대금·예정 소유비율.
    양도인만 계약당사자 표의 셀 정렬이 서식마다 어긋나, '변경전 최대주주' 값을 갖는
    첫 이름 키로 best-effort 추출(실패 시 생략, 양수인은 항상 표시)."""
    lines = []

    buyer = _get(kv, '3. 변경예정 최대주주', '변경예정 최대주주', '-양수인')
    # 일부 정정 서식에서 셀 밀림으로 이름 뒤에 날짜·수량이 붙음
    # ('지서현 2026-08-13 3,823,859 7.49') → 이름만 (날짜·3자리+숫자 앞에서 절단)
    if buyer:
        buyer = re.split(r'\s+(?=\d{4}-\d{2}-\d{2}|[\d,]{3,}\b)', buyer)[0].strip()
    seller = None
    for k, v in kv.items():
        if (v and v.strip() == '변경전 최대주주'
                and not k.startswith(('-', '1.', '2.', '3.', '4.', '5.', '성명', '변경'))
                and '관계' not in k and '주식' not in k):
            seller = k.strip()
            break

    if seller or buyer:
        lines.append('🔄 최대주주 변경 (주식양수도)')
        if seller:
            lines.append(f'  양도인: {_trunc(seller, 30)}')
        if buyer:
            lines.append(f'  양수인: {_trunc(buyer, 30)}')

    # 거래 규모
    shares = _get(kv, '양수도 주식수(주)', '양수도주식수')
    price  = _get(kv, '1주당 가액(원)', '1주당가액')
    amount = _get(kv, '양수도 대금(원)', '양수도대금')
    if shares and re.search(r'\d', shares):
        ps = f' (주당 {price}원)' if price else ''
        lines.append(f'🔢 양수도 주식: {int(re.sub(r"[^0-9]", "", shares)):,}주{ps}')
    if amount:
        lines.append(f'💰 양수도 대금: {_fmt_amount(amount)}원')

    # 변경 후 지분 (양수인 인수 후)
    a_sh = _get(kv, '-예정 소유주식수(주)', '예정 소유주식수')
    a_rt = _get(kv, '-예정 소유비율(%)', '예정 소유비율')
    if a_sh or a_rt:
        parts = []
        if a_sh and re.search(r'\d', a_sh):
            parts.append(f'{int(re.sub(r"[^0-9]", "", a_sh)):,}주')
        if a_rt:
            parts.append(f'{a_rt}%')
        if parts:
            lines.append(f'📊 변경후 지분: {" / ".join(parts)}')

    if v := _get(kv, '-변경 예정일자', '변경 예정일자'):
        lines.append(f'📅 변경예정일: {v}')
    if v := _get(kv, '4. 계약일자', '계약일자'):
        lines.append(f'📝 계약일자: {v}')
    if v := _get(kv, '관련공시', '※관련공시', '※ 관련공시'):
        lines.append(f'🔗 관련: {_trunc(v, 50)}')

    return lines
