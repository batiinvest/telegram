"""
dart_parsers_holdings.py — 지분·대량보유·공개매수·담보·양수도 관련 공시 파서
(2026-07 dart_parsers_governance 세분화 — 파서 원문 무변경 이식)
"""
import re  # noqa: F401
from dart_parse_helpers import *  # noqa: F401,F403  헬퍼·상수·_get/_trunc·log


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

    # 보유목적 (API에 없으므로 HTML KV 사용) — 코드('02' 등)면 HTML 디코딩 라벨로 변환
    purpose = _get(kv, 'Purpose of holding', '보유목적', '주식등의보유목적', '보유 목적')
    if purpose and re.fullmatch(r'\d+', purpose.strip()):
        _txt = re.sub(r'<[^>]+>', ' ', kv.get('_html', ''))
        _m = re.search(r'보유\s*목적[\s:]*([가-힣]{2,10})', _txt)
        purpose = (_m.group(1) if _m else
                   {'01': '경영참여', '02': '단순투자', '03': '일반투자'}.get(purpose.strip(), purpose))
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
            _sc = api.get('stkqy_irds', '')
            _tail = f' (증감 {_sc}주)' if _sc and _sc not in ('-', '0', '') else ''
            lines.append(f'🔢 보유주식: {stkqy}주{_tail}')
        if ctr_stkrt and ctr_stkrt not in ('0', '-', ''):
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

    # '특수관계인' 제외 — '변경후 최대주주의 특수관계인'이 '변경후 최대주주'를
    # 부분매칭해 컨소시엄(외 N인) 인수 시 소수지분 특수관계인을 최대주주로 오선정하던 문제.
    for idx, (k, v) in enumerate(items):
        if k.startswith('_'):
            continue
        if ('변경전 최대주주' in v and '특수관계인' not in v
                and k not in ('-', '', '성명(법인명,조합명,기타단체명)')):
            before_name = k
            if idx + 1 < len(items):
                nk, nv = items[idx + 1]
                if re.match(r'^[\d,]+$', nk) and re.match(r'^[\d.]+$', nv):
                    before_shares, before_ratio = nk, nv
        elif '변경후 최대주주' in v and '특수관계인' not in v and k not in ('-', '', '변경후'):
            after_name = k
            if idx + 1 < len(items):
                nk, nv = items[idx + 1]
                if re.match(r'^[\d,]+$', nk) and re.match(r'^[\d.]+$', nv):
                    after_shares, after_ratio = nk, nv

    # 다자 컨소시엄 등으로 변경후 최대주주(리드)를 못 찾으면 요약('외 N인')으로 대체 —
    # 관계셀이 국적으로 밀리는 서식에서도 정확한 신규 최대주주등을 표시.
    if not after_name:
        summ = _get(kv, '정정후_변경후', '변경후 최대주주등', '변경후최대주주등')
        if summ and summ not in ('최대주주등', '변경후', '-', ''):
            after_name = summ  # 요약이라 단일 지분율은 없음

    if before_name or after_name:
        lines.append('🔄 최대주주 변경')
        if before_name:
            detail = f' ({before_shares}주 / {before_ratio}%)' if before_shares else ''
            lines.append(f'  변경전: {before_name}{detail}')
        if after_name:
            detail = f' ({after_shares}주 / {after_ratio}%)' if after_shares else ''
            lines.append(f'  변경후: {after_name}{detail}')

    # 변경사유 — 선두 대시 제거 + ' - ' 절 분리(거래내용/부가설명). 종전 60자
    # 절단은 '…및 한앤…'처럼 인수 상대·주식수를 잘라 핵심 거래를 잃었음.
    if v := _get(kv, '2. 변경사유', '변경사유'):
        v = re.sub(r'^\s*[-·•]\s*', '', re.sub(r'\s+', ' ', v).strip())
        subs = [s.strip() for s in re.split(r'\s+-\s+', v) if s.strip()]
        if len(subs) >= 2:
            lines.append('📋 사유:')
            for s in subs[:3]:
                lines.append(f'  • {_trunc_clean(s, 150)}')
        else:
            lines.append(f'📋 사유: {_trunc_clean(v, 150)}')

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


def parse_tender_opinion(kv: dict) -> list:
    """공개매수에관한의견표명서 — 대상회사가 공개매수에 밝히는 찬반 의견.

    의견(찬성/반대/중립)은 KV 표엔 없고 산문 본문에 있음. 반대=경영권 방어 신호로
    중요. 회사 자체 의견과 '주주 응모 권고'는 다를 수 있어(회사 찬성·주주 중립 등) 분리.
    """
    _OP = {'찬성': '✅ 찬성', '반대': '🚫 반대', '중립': '⚖️ 중립', '유보': '⚖️ 유보'}
    lines = ['📢 공개매수 의견표명']

    if v := _get(kv, '성 명 :', '성명'):
        v = re.sub(r'^[ㆍ·\s]*성\s*명\s*[:：]\s*', '', v).strip()
        lines.append(f'🏢 공개매수자: {_trunc(v, 30)}')
    if v := _get(kv, '회 사 명 :', '회사명'):
        lines.append(f'🎯 대상: {_trunc(v, 30)}')

    txt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', kv.get('_html', '')))
    if m := re.search(r'공개매수에 대하여\s*(찬성|반대|중립|유보)', txt):
        lines.append(f'📋 회사 의견: {_OP.get(m.group(1), m.group(1))}')
    if m := re.search(r'응할지\s*여부에\s*대해서는\s*(찬성|반대|중립|유보)', txt):
        lines.append(f'👥 주주 응모 권고: {_OP.get(m.group(1), m.group(1))}')

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


def _lead_int(s):
    """콤마 포함 정수 문자열에서 선두 정수만 안전 추출.
    셀 밀림으로 여러 숫자 토큰이 뭉친 값이면(신뢰 불가) None 반환."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r'[0-9,]+', s)
    if not m:
        return None
    rest = s[m.end():].lstrip()
    if rest[:1].isdigit() or rest[:1] == '.':
        return None  # 다음 토큰도 숫자 → 셀 밀림
    return int(m.group(0).replace(',', ''))


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
    shares_n = _lead_int(shares)
    if shares_n is None:
        shares_n = _lead_int(_get(kv, '공시일 현재'))  # 정정 서식 셀밀림 폴백
    if shares_n is not None:
        if exec_m:
            exec_n = int(exec_m.group(0).replace(',', ''))
            lines.append(f'📊 보유주식: {shares_n:,}주 → 담보실행시 {exec_n:,}주')
        else:
            ratio = _get(kv, '지분율(%)', '지분율')
            ratio_str = f' ({ratio}%)' if ratio else ''
            lines.append(f'📊 보유지분: {shares_n:,}주{ratio_str}')

    # 채무금액 / 담보설정금액
    debt = _get(kv, '2. 채무(차입)금액 총액(원)', '채무(차입)금액 총액(원)', '채무금액')
    coll = _get(kv, '3. 담보설정금액 총액(원)', '담보설정금액 총액(원)', '담보설정금액')
    if debt:
        lines.append(f'💸 채무금액: {_fmt_amount(debt)}원')
    if coll:
        lines.append(f'🔒 담보설정: {_fmt_amount(coll)}원')

    # 담보제공 주식수 (누적)
    pledge_n = _lead_int(_get(kv, '누적 담보제공 주식 총수(주)', '담보제공주식수(주)'))
    if pledge_n is not None:
        lines.append(f'📌 담보주식: {pledge_n:,}주')

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
