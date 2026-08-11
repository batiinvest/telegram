"""
dart_parsers_capital.py — 자본·주식·배당 관련 공시 파서 (증자·감자·배당·자기주식·전환사채·스톡옵션)
(2026-07 dart_parsers 분할 — 파서 원문 무변경 이식)
"""
import re  # noqa: F401
from dart_parse_helpers import *  # noqa: F401,F403  헬퍼·상수·_get/_trunc·log


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

    # ── 한글 번호 키 서식 폴백 (종속회사의 주요경영사항 유상증자 등) ──
    # 이 서식은 '5. 증자방식'처럼 번호 접두 한글 키라 위 영문 _f가 전부 실패 →
    # 범용 폴백이 raw KV(번호 키 그대로)를 덤프하던 문제. 핵심 필드만 구조화.
    if not lines:
        if sub := _get(kv, '종속회사인', '종속회사명'):
            lines.append(f'🏢 종속회사: {_trunc(sub, 50)}')
        if v := _get(kv, '보통주식(주)'):
            lines.append(f'🔢 신주식수: {v}주')
        if v := _get(kv, '보통주식(원)', '예정발행가'):
            _hint = ' (액면가)' if '액면가' in (_get(kv, '7. 발행가 산정방법') or '') else ''
            lines.append(f'💵 발행가액: {v}원{_hint}')
        if v := _get(kv, '5. 증자방식', '증자방식'):
            lines.append(f'📋 방식: {v}')
        for fk, flabel in (('시설자금(원)', '시설'), ('영업양수자금(원)', '영업양수'),
                           ('운영자금(원)', '운영'), ('채무상환자금(원)', '채무상환'),
                           ('타법인 증권 취득자금(원)', '타법인취득'), ('기타자금(원)', '기타')):
            v = _get(kv, fk)
            if v and v.replace(',', '').strip() not in ('', '-', '0'):
                lines.append(f'💰 조달: {flabel}자금 {_fmt_amount(v)}원')
                break
        if v := _get(kv, '8. 신주배정기준일', '신주배정기준일'):
            lines.append(f'📅 신주배정기준일: {v}')
        s, e = _get(kv, '시작일'), _get(kv, '종료일')
        if s and s != '-':
            lines.append(f'📅 청약: {s}' + (f' ~ {e}' if e and e not in ('-', s) else ''))
        if v := _get(kv, '18. 이사회결의일(결정일)', '이사회결의일(결정일)'):
            lines.append(f'📋 결의일: {v}')

    return lines


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


def parse_bond_acquisition(kv: dict) -> list:
    """발행 후 만기전 사채취득 — 콜옵션/조기상환 등으로 CB·BW를 만기 전 취득(자기사채).

    발행사가 콜옵션(매도청구권)을 행사해 CB를 조기 취득하면 잠재 희석 물량이
    소멸 → 오버행 축소. 취득 권면총액·취득후 잔여·전환가·사유(콜/풋)를 요약.
    한글 번호 키 서식이라 parse_cb(영문 발행 서식)로는 안 잡혀 폴백으로 새던 유형."""
    lines = []

    rnd = _get(kv, '전환사채(해외전환사채)', '신주인수권부사채', '회차')
    hdr = '🔄 만기전 사채취득'
    if rnd:
        hdr += f' ({rnd.strip()})'
    lines.append(hdr)

    if kind := _get(kv, '사채의 종류'):
        lines.append(f'📋 종류: {_trunc(kind, 40)}')

    # 취득 권면총액(+취득금액) — 취득금액은 원금+이자라 권면과 다를 수 있음
    face = _get(kv, '- 취득한 사채의 권면(전자등록)총액 (통화단위)',
                '취득한 사채의 권면(전자등록)총액')
    amt = _get(kv, '2. 사채 취득금액 (통화단위)', '사채 취득금액')
    if face:
        line = f'💰 취득 권면총액: {_fmt_amount(face)}원'
        if amt and amt.replace(',', '') != face.replace(',', ''):
            line += f' (취득금액 {_fmt_amount(amt)}원)'
        lines.append(line)
    elif amt:
        lines.append(f'💰 취득금액: {_fmt_amount(amt)}원')

    if rest := _get(kv, '3. 취득후 사채의 권면(전자등록)총액 (통화단위)',
                    '취득후 사채의 권면(전자등록)총액'):
        lines.append(f'📊 취득후 잔여: {_fmt_amount(rest)}원')

    if v := _get(kv, '- 취득일자', '취득일자'):
        lines.append(f'📅 취득일: {v}')

    conv = _get(kv, '주당 전환가액(원)', '전환가액(원)')
    mat = _get(kv, '만기일')
    if conv:
        lines.append(f'💵 전환가: {conv}원' + (f' / 만기 {mat}' if mat else ''))

    if reason := _get(kv, '4. 만기전 취득사유 및 향후 처리방법',
                      '만기전 취득사유 및 향후 처리방법'):
        bullets = _parse_numbered_body(reason, max_items=4)
        if bullets:
            lines.append('📋 취득사유:')
            lines.extend(bullets)
        else:
            lines.append(f'📋 취득사유: {_trunc_clean(reason, 150)}')

    tail = [x for x in (_get(kv, '6. 사채의 취득방법', '사채의 취득방법'),
                        _get(kv, '5. 취득자금의 원천', '취득자금의 원천')) if x]
    if tail:
        lines.append(f'📋 방법: {" · ".join(tail)}')

    # 헤더 외 실제 값이 하나도 없으면(서식 상이) 폴백에 위임
    return lines if len(lines) > 1 else []


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


def parse_subscription_result(kv: dict) -> list:
    """유상증자또는주식관련사채등의 청약결과 / 발행결과(자율공시).

    구(舊): 제목에 '유상증자'가 있어 parse_rights_offering(증자결정용, 신주식수·
    발행가액 등 다른 필드)로 매칭 → 빈 결과 → 폴백 노이즈(번호 필드+boilerplate).
    - 청약결과: 청약률(≥100%=완전청약/초과, <100%=미달)이 핵심.
    - 발행결과: 실제발행금액(조달 완료)·납입일이 핵심. 실제<예정이면 미달.
    """
    def _num(s):
        d = re.sub(r'[^0-9]', '', s or '')
        return int(d) if d else None

    actual_amt = _get(kv, '실제발행금액(원)', '실제발행금액')
    is_issue = bool(actual_amt)   # 발행결과 vs 청약결과 구분

    lines = [f'📢 유상증자 {"발행결과" if is_issue else "청약결과"}']

    method = _get(kv, '2. 발행방법', '발행방법')
    kind   = _get(kv, '1. 증권의 종류', '증권의 종류')
    hdr = ' · '.join([x for x in (method, kind) if x])
    if hdr:
        lines.append(f'📋 {_trunc(hdr, 40)}')

    if v := _get(kv, '발행예정주식수(주)', '발행예정주식수'):
        if n := _num(v):
            lines.append(f'🔢 발행예정: {n:,}주')

    if is_issue:
        # ── 발행결과 ──
        if v := _get(kv, '실제발행주식수(주)', '실제발행주식수'):
            if n := _num(v):
                lines.append(f'✅ 실제발행: {n:,}주')
        if m := re.search(r'[\d,]{4,}', actual_amt):
            planned = _get(kv, '발행예정금액(원)', '발행예정금액')
            short = ''
            if planned and (pn := _num(planned)) and (an := _num(actual_amt)) and an < pn:
                short = ' (예정 대비 미달)'
            lines.append(f'💰 조달금액: {_fmt_amount(m.group(0))}원{short}')
        if v := _get(kv, '납입일'):
            lines.append(f'📅 납입일: {_trunc(v, 30)}')
    else:
        # ── 청약결과 ──
        if v := _get(kv, '청약주식수(누계)(주)', '청약주식수(누계)'):
            if n := _num(v):
                lines.append(f'✅ 청약주식수: {n:,}주 (누계)')
        if v := _get(kv, '청약률(%)', '청약률'):
            if m := re.search(r'[\d.]+', v):
                lines.append(f'📊 청약률: {m.group(0)}%')
        if v := _get(kv, '3. 청약대상자', '청약대상자'):
            lines.append(f'👥 대상: {_trunc(v, 40)}')
        if v := _get(kv, '4. 청약일자', '청약일자'):
            lines.append(f'📅 청약일: {_trunc(v, 30)}')

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


def parse_dividend(kv: dict) -> list:
    """현금ㆍ현물배당결정 / 금전배당결정 — 배당구분·1주당 배당금·시가배당률·기준일·지급일"""
    lines = []

    kind  = _get(kv, '1. 배당구분', '배당구분')
    kind2 = _get(kv, '2. 배당종류', '배당종류')
    parts = [p for p in (kind, kind2) if p]
    if parts:
        lines.append('💰 ' + ' · '.join(parts))

    # 1주당 배당금 + 시가배당률 — 값이 '보통주식 233' 형태
    per  = _get(kv, '1주당 배당금(원)', '1주당 배당금', '1주당배당금')
    rate = _get(kv, '시가배당률(%)', '시가배당율(%)', '시가배당률')
    if per:
        m  = re.search(r'보통주[식]?\s*([\d,]+)', per) or re.search(r'([\d,]+)', per)
        mr = (re.search(r'보통주[식]?\s*([\d.]+)', rate or '')
              or re.search(r'([\d.]+)', rate or ''))
        if m:
            rate_str = f' (시가배당률 {mr.group(1)}%)' if mr else ''
            lines.append(f'💵 1주당 {m.group(1)}원{rate_str}')

    _f(lines, kv, '💰 배당총액', '배당금총액(원)', '배당금총액', fmt=_fmt_amount, suffix='원')
    _f(lines, kv, '📦 현물자산', '현물자산의 상세내역', trunc=50)
    _f(lines, kv, '📅 배당기준일', '배당기준일')
    if v := _get(kv, '배당금지급 예정일자', '배당금지급 예정일', '지급예정일'):
        lines.append(f'📅 지급예정: {_trunc(v, 40)}')
    _f(lines, kv, '📋 결의일', '이사회결의일(결정일)', '이사회결의일')

    return lines


def parse_share_cancellation(kv: dict) -> list:
    """주식소각결정 — 소각주식수·소각금액·취득방법·소각예정일"""
    lines = []

    # 소각 주식수: 헤더 짝밀림('1. 소각할…' → '보통주식(주)' → 수량) 구조 대응
    cnt = None
    for k, v in kv.items():
        if k.startswith('_') or '정정' in k:
            continue
        if '보통주' in k and re.match(r'^[\d,]+$', (v or '').strip()):
            cnt = v.strip()
            break
    if not cnt:
        v = _get(kv, '소각할 주식의 종류와 수')
        m = re.search(r'([\d,]{4,})', v or '')
        cnt = m.group(1) if m else None
    if cnt:
        lines.append(f'🔥 소각주식: 보통주 {cnt}주')

    _f(lines, kv, '💰 소각예정금액', '소각예정금액(원)', '소각예정금액', fmt=_fmt_amount, suffix='원')
    _f(lines, kv, '📋 취득방법', '소각할 주식의 취득방법', '취득방법', trunc=40)
    _f(lines, kv, '📅 소각예정일', '소각 예정일', '소각예정일')
    _f(lines, kv, '📋 결의일', '이사회결의일(결정일)', '이사회결의일')

    return lines


def parse_capital_reduction_done(kv: dict) -> list:
    """감자완료 — 완료일·감자비율·발행주식/자본금 전후"""
    lines = []

    _f(lines, kv, '📅 감자완료일', '감자 완료일', '감자완료일')

    # 감자비율: '대주주 80' + 별도 '소액주주' 행
    ratio = _get(kv, '감자비율(%)', '감자비율')
    minor = _get(kv, '소액주주')
    if ratio:
        m = re.search(r'([\d.]+)', ratio)
        if m:
            if minor and re.match(r'^[\d.]+$', minor) and minor != m.group(1):
                lines.append(f'📉 감자비율: 대주주 {m.group(1)}% · 소액주주 {minor}%')
            else:
                lines.append(f'📉 감자비율: {m.group(1)}%')

    # 발행주식 전→후: '-보통주식(주)'(감자전) 다음 [감자전: 감자후] 숫자쌍 행
    items = list(kv.items())
    for idx, (k, v) in enumerate(items):
        if k.replace(' ', '').startswith('-보통주식') and re.match(r'^[\d,]+$', (v or '').strip()):
            pre, post = v.strip(), None
            if idx + 1 < len(items):
                k2, v2 = items[idx + 1]
                if re.match(r'^[\d,]+$', k2.strip()) and re.match(r'^[\d,]+$', (v2 or '').strip()):
                    post = v2.strip()
            lines.append(f'📦 발행주식: {pre} → {post}주' if post else f'📦 감자전 발행주식: {pre}주')
            break

    # 자본금 전→후: '4. 자본금 변동(원)' 값(감자전)이 다음 행의 키로 반복되는 구조
    cap_pre = _get(kv, '자본금 변동(원)', '자본금 변동')
    if cap_pre and re.match(r'^[\d,]+$', cap_pre):
        cap_post = (kv.get(cap_pre) or '').strip()
        if re.match(r'^[\d,]+$', cap_post):
            lines.append(f'💰 자본금: {_fmt_amount(cap_pre)}원 → {_fmt_amount(cap_post)}원')
        else:
            lines.append(f'💰 감자전 자본금: {_fmt_amount(cap_pre)}원')

    _f(lines, kv, '📋 감자결정일', '감자결정 이사회결의일')

    return lines


def parse_conversion_adjust(kv: dict) -> list:
    """전환/신주인수권 행사/교환 가액의 조정 — 조정전→후 가액·사유·적용일.

    서식 3형 대응: ①2열(조정전/후 키에 값 직접) ②복합값('9,714 8,538')
    ③다회차 짝밀림(가액쌍이 [조정전: 조정후] 숫자행으로 등장, 회차별 복수)
    """
    lines = []

    def _to_int(s):
        try:
            return int(s.replace(',', ''))
        except (ValueError, AttributeError):
            return None

    pairs = []           # (조정전, 조정후)
    used_keys: set = set()

    # ① 직접 키
    pre  = _get(kv, '조정전 전환가액', '조정전 행사가액', '조정전 교환가액')
    post = _get(kv, '조정후 전환가액', '조정후 행사가액', '조정후 교환가액')
    m_pre  = re.search(r'([\d,]{3,})', pre or '')
    m_post = re.search(r'([\d,]{3,})', post or '')
    if m_pre and m_post:
        pairs.append((m_pre.group(1), m_post.group(1)))

    # ② 복합값 '조정전 조정후' — 한 셀에 두 숫자
    if not pairs:
        for k, v in kv.items():
            if k.startswith('_'):
                continue
            m = re.fullmatch(r'([\d,]{3,})\s+([\d,]{3,})', (v or '').strip())
            if m:
                pairs.append((m.group(1), m.group(2)))
                used_keys.add(k)
                break

    # ③ 다회차 짝밀림: [조정전: 조정후] 숫자행 — 전환가액 범위(100원~1000만원)만
    if not pairs:
        for k, v in kv.items():
            a, b = _to_int(k.strip()), _to_int((v or '').strip())
            if a and b and a != b and 100 <= a <= 10_000_000 and 100 <= b <= 10_000_000:
                pairs.append((k.strip(), (v or '').strip()))
                used_keys.add(k)
                if len(pairs) >= 3:
                    break

    for pre_n, post_n in pairs[:3]:
        a, b = _to_int(pre_n), _to_int(post_n)
        if a and b and a > 0:
            lines.append(f'💵 전환가액: {pre_n}원 → {post_n}원 ({(b - a) / a * 100:+.1f}%)')
        else:
            lines.append(f'💵 전환가액: {pre_n}원 → {post_n}원')

    # 전환가능주식수 전→후: [전: 후] 숫자행 (1만주 이상, 가액쌍으로 안 쓴 행)
    for k, v in kv.items():
        if k in used_keys:
            continue
        a, b = _to_int(k.strip()), _to_int((v or '').strip())
        if a and b and a != b and a >= 10_000 and b >= 10_000:
            lines.append(f'🔢 전환가능주식: {k.strip()} → {(v or "").strip()}주')
            break

    _f(lines, kv, '📋 사유', '조정사유', trunc=70)
    _f(lines, kv, '📅 적용일', '조정가액 적용일')

    return lines
