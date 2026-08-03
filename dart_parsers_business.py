"""
dart_parsers_business.py — 계약·실적·재무 관련 공시 파서 (공급계약·잠정실적·손익구조·채무보증·파생)
(2026-07 dart_parsers 분할 — 파서 원문 무변경 이식)
"""
import re  # noqa: F401
from dart_parse_helpers import *  # noqa: F401,F403  헬퍼·상수·_get/_trunc·log


def parse_contract(kv: dict) -> list:
    """단일판매ㆍ공급계약체결 / 수주"""
    lines = []

    # 계약명 — 신서식은 '1. 판매ㆍ공급계약 내용'이 계약 주제(구서식 체결계약명/계약명)
    if v := _get(kv, '체결계약명', '계약명',
                 '판매ㆍ공급계약 내용', '판매·공급계약 내용', '공급계약 내용', '수주 내용'):
        lines.append(f'📋 계약명: {_trunc(v, 60)}')

    # 계약상대 + 지역 — 각주(1. 적용환율... 형태) 필터링
    party  = _get(kv, '계약상대', '거래상대방', '발주처', '매수인')
    region = _get(kv, '판매ㆍ공급지역', '공급지역', '수주지역', '납품지역')
    if party and not _is_footnote(party):
        party_clean = _clean_party(party)
        region_str = f' ({_trunc(region, 30)})' if region and not _is_footnote(region) else ''
        lines.append(f'🏢 상대방: {party_clean}{region_str}')

    # 계약금액 + 매출비중 — '정정전/후' 복합값(금액 비율)에서 각각 분리
    # ※ '계약금액 총액' 최우선: 조건부 계약 서식은 [확정 계약금액 0 / 조건부 계약금액 N /
    #    계약금액 총액 N] 구조라, '계약금액' 부분일치가 '확정 계약금액'(0)에 먼저 걸려
    #    "0원"으로 오표기되던 버그(2026-07-22 스트라드비젼 실사례).
    amount = _get(kv, '계약금액 총액', '계약금액(원)', '계약금액',
                  '공급금액', '수주금액', '거래금액')
    # 조건부 계약 여부 — 확정금액이 0이고 조건부 금액이 있으면 미확정 리스크 고지
    _fixed_amt = _get(kv, '확정 계약금액')
    _cond_amt  = _get(kv, '조건부 계약금액')
    cond_note = (' · 조건부계약'
                 if _cond_amt and _fixed_amt
                 and re.fullmatch(r'0+', _fixed_amt.replace(',', '').strip() or 'x')
                 else '')
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
        lines.append(f'💰 계약금액: {_fmt_amount(amt_clean)}원{ratio_str}{cond_note}')

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


def _parse_auto_sales(html):
    """자동차 등 '단위:대' 판매실적 표 파싱 (손익 공란이어도 판매량이 실적내용:
    현대차·기아 월 판매실적 등). 국내/해외/계 행에서 당기값(idx1)·전년동기대비%(idx6).
    반환: (당월라벨, {지역:(수량,YoY)}, 누적라벨, {지역:(수량,YoY)})."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or '', 'html.parser')
    mode = None
    month, ytd = {}, {}
    m_lbl, y_lbl = '', ''
    for tr in soup.find_all('tr'):
        cells = [re.sub(r'\s+', ' ', c.get_text(' ', strip=True)).strip()
                 for c in tr.find_all(['td', 'th'])]
        joined = ' '.join(cells)
        if re.search(r'단위\s*[:：]\s*대', joined):
            if '누적' in joined or '누계' in joined:
                mode = 'ytd'
                mm = re.search(r'당기누적\s*\(([^)]+)\)', joined)
                y_lbl = mm.group(1).strip() if mm else ''
            else:
                mode = 'month'
                mm = re.search(r'당기실적\s*\(([^)]+)\)', joined)
                m_lbl = mm.group(1).strip() if mm else ''
            continue
        if mode and cells and cells[0] in ('국내', '해외', '계'):
            qty = cells[1] if len(cells) > 1 else ''
            yoy = cells[6] if len(cells) > 6 else ''
            if qty and qty not in ('-', ''):
                (month if mode == 'month' else ytd)[cells[0]] = (qty, yoy)
    return m_lbl, month, y_lbl, ytd


def parse_preliminary_earnings(kv: dict) -> list:
    """연결/별도 잠정실적 공정공시"""
    lines = []

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

    # 단위 감지 — 서식이 억원/백만원/천원/원 중 선택. 구: 백만원 고정 →
    # 억원으로 보고하는 대형사(HD현대중공업 등)가 100배 축소 발송되던 버그.
    _unit_mult = 1_000_000   # 기본 백만원 (단위 미표기 시 종전 동작 유지)
    _UNIT_MULT = {'억원': 100_000_000, '백만원': 1_000_000, '천원': 1_000, '원': 1}
    for _k, _v in kv.items():
        m = re.search(r'단위\s*[:：]?\s*(억원|백만원|천원|원)', f'{_k} {_v}')
        if m:
            _unit_mult = _UNIT_MULT[m.group(1)]
            break

    def _fmt_amt(v: str) -> str:
        try:
            n = int(v.replace(',', ''))
            return _fmt_amount(str(abs(n) * _unit_mult))
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

    # ── 판매대수 표(자동차 월 판매 등 '단위:대') — 손익 공란이어도 판매량이 실적내용 ──
    _msl, _mon, _ysl, _ytd = _parse_auto_sales(kv.get('_html', ''))
    if _mon.get('계') or _ytd.get('계'):
        def _yoy(v):
            v = (v or '').strip()
            if not v or v in ('-', ''):
                return ''
            return ' (YoY ' + (v if v.startswith('-') else '+' + v) + '%)'
        lines.append('🚗 판매실적' + ((' (' + _msl + ')') if _msl else ''))
        for _rg in ('국내', '해외', '계'):
            if _rg in _mon:
                _q, _y = _mon[_rg]
                lines.append('  ' + ('합계' if _rg == '계' else _rg) + ': ' + _q + '대' + _yoy(_y))
        if '계' in _ytd:
            _q, _y = _ytd['계']
            lines.append('  누적' + (('(' + _ysl + ')') if _ysl else '') + ': ' + _q + '대' + _yoy(_y))

    return lines


def extract_preliminary_current(kv: dict) -> dict | None:
    """잠정실적 KV에서 이번 분기 구조화 값 추출 (추이 표시용 — 이번 분기는 financials
    미반영이라 여기서 뽑아 붙임). parse_preliminary_earnings와 동일 단위·인덱스 로직.
    반환: {'year','quarter'(1~4),'label'('26.2Q'),'revenue','operating_profit','net_income'}
    (금액은 원 단위). 핵심 지표 없으면 None."""
    # 연도·분기 — 당기실적 시작일(2026-04-01→Q2) 우선, 없으면 레이블 정규식
    year = q = None
    period = _get(kv, '당기실적')
    m = re.match(r'(\d{4})-(\d{2})', period or '')
    if m:
        year, q = int(m.group(1)), (int(m.group(2)) - 1) // 3 + 1
    if year is None:
        for k in kv:
            mm = re.match(r"^\('?([0-9]{2})\.([0-9])Q?\)", k)
            if mm:
                year, q = 2000 + int(mm.group(1)), int(mm.group(2)); break
            mm2 = re.match(r'^\(([0-9]{4})년\s*([0-9])분기\)', k)
            if mm2:
                year, q = int(mm2.group(1)), int(mm2.group(2)); break

    _unit_mult = 1_000_000
    _UM = {'억원': 100_000_000, '백만원': 1_000_000, '천원': 1_000, '원': 1}
    for _k, _v in kv.items():
        mu = re.search(r'단위\s*[:：]?\s*(억원|백만원|천원|원)', f'{_k} {_v}')
        if mu:
            _unit_mult = _UM[mu.group(1)]; break

    items = list(kv.items())
    _IS_NUM = re.compile(r'^-?[\d,]+(\.\d+)?$')
    _MAP = {'매출액': 'revenue', '영업이익': 'operating_profit', '당기순이익': 'net_income'}
    out = {'year': year, 'quarter': q,
           'label': f'{str(year)[2:]}.{q}Q' if (year and q) else ''}
    for idx, (k, _v) in enumerate(items):
        if k in _MAP and idx + 1 < len(items):
            k1 = items[idx + 1][0]
            if _IS_NUM.match(k1):
                try:
                    out[_MAP[k]] = int(k1.replace(',', '')) * _unit_mult
                except ValueError:
                    pass
    return out if any(x in out for x in ('revenue', 'operating_profit', 'net_income')) else None


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


def parse_earnings_change(kv: dict) -> list:
    """매출액또는손익구조 30%(대규모법인 15%)이상 변경 — 당해 실적·증감률 요약.

    다열 표 짝밀림 구조: ('- 매출액': 당해값) → (직전값: 증감금액) → (증감비율: 흑전여부)
    순서 기반 추출 — 정규식 가드 실패 시 해당 필드만 생략 (지표 0개면 범용 폴백).
    """
    lines = []

    ftype = _get(kv, '재무제표의 종류')
    lines.append('📊 손익구조 변동' + (f' ({ftype})' if ftype else ''))

    # 단위 감지 (서식 기본 천원 — '단위:원' 명시 시에만 1배)
    unit_mult = 1000
    for k in kv:
        if '단위' in k and '천원' not in k and re.search(r'단위\s*[:：]\s*원', k):
            unit_mult = 1
            break

    items = list(kv.items())
    _NUM = re.compile(r'^-?[\d,]+$')
    _PCT = re.compile(r'^-?\d{1,4}(\.\d+)?$')
    LABELS = {'매출액': '매출', '영업이익': '영업이익',
              '법인세비용차감전계속사업이익': '세전이익', '당기순이익': '순이익'}
    seen_metric: set = set()

    for idx, (k, v) in enumerate(items):
        name = re.sub(r'^[-\s]+', '', k).strip()
        if name not in LABELS or name in seen_metric:
            continue
        if not _NUM.match((v or '').replace(' ', '')):
            continue
        seen_metric.add(name)
        curr = int(v.replace(',', ''))

        pct = flip = ''
        if idx + 2 < len(items):
            k3, v3 = items[idx + 2]
            # (증감비율: 흑전여부) 행 — 직전 행이 (직전값: 증감금액) 숫자쌍일 때만 신뢰
            if _NUM.match(items[idx + 1][0].replace(' ', '')) and _PCT.match(k3.strip()):
                pct = k3.strip()
                if v3 and ('흑자' in v3 or '적자' in v3):
                    flip = v3.strip()

        amt = ('-' if curr < 0 else '') + _fmt_amount(str(abs(curr) * unit_mult))
        line = f'  {LABELS[name]}: {amt}원'
        extra = []
        if pct:
            extra.append(('+' if not pct.startswith('-') else '') + pct + '%')
        if flip:
            extra.append(flip)
        if extra:
            line += ' (' + ' · '.join(extra) + ')'
        lines.append(line)

    if len(lines) == 1:      # 지표 추출 실패 — 범용 폴백에 위임
        return []

    if v := _get(kv, '변동 주요원인'):
        reason = _trunc_clean(re.sub(r'\s+', ' ', v), 120)
        lines.append(f'📋 원인: {reason}')
    _f(lines, kv, '📋 결의일', '이사회결의일(결정일)', '이사회결의일')

    return lines
