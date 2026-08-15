"""
dart_parsers_status.py — 거래정지·신탁·제재·정정 관련 공시 파서
(2026-07 dart_parsers 분할 — 파서 원문 무변경 이식)
"""
import re  # noqa: F401
from dart_parse_helpers import *  # noqa: F401,F403  헬퍼·상수·_get/_trunc·log


def parse_trading_halt(kv: dict) -> list:
    """주권매매거래정지 / 기간변경 / 해제"""
    lines = []

    # ── 거래정지 해제 형식 ───────────────────────────────────────
    release = _get(kv, '2.해제사유', '해제사유')
    if release:
        etc = _get(kv, '5.기타', '기타') or ''
        # 상장폐지 정리매매 개시 — '해제'지만 실질은 상장폐지(최악 이벤트).
        # 🔓(해제) 대신 🚨로, 핵심 일정(정리매매기간·상장폐지일)을 별도 노출.
        is_delist = '상장폐지' in release or '정리매매' in release or '상장폐지' in etc
        lines.append(f'{"🚨" if is_delist else "🔓"} {release}')
        if v := _get(kv, '1.대상종목', '대상종목'):
            lines.append(f'📋 대상: {v}')
        halt_dt = _get(kv, '3.해제일시', '해제일시') or ''
        if halt_dt:
            label = '📅 정리매매 개시' if is_delist else '📅 해제일시'
            lines.append(f'{label}: {halt_dt.rstrip(" -").strip()}')
        if is_delist and etc:
            mp = re.search(r'정리매매기간\s*[:：]?\s*([\d.]+\s*~\s*[\d.]+(?:\([^)]*\))?)', etc)
            md = re.search(r'상장폐지일\s*[:：]?\s*([\d.]+)', etc)
            mr = re.search(r'상장폐지\s*사유\s*[:：]?\s*(.+?)(?=\s*-\s*정리매매|\s*-\s*상장폐지일|$)', etc)
            if mp:
                lines.append(f'🕐 정리매매: {mp.group(1)}')
            if md:
                lines.append(f'🔚 상장폐지일: {md.group(1)}')
            if mr:
                lines.append(f'📋 사유: {_trunc(mr.group(1).strip(), 60)}')
        elif etc:
            lines.extend(_parse_etc_field(etc))
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
                    # 제목 ↔ 본문 분리 — 제목 끝은 ① 제목 종결 괄호 ')'(핀텔형) 또는
                    # ② 본문 시작 날짜('26.MM.DD, 자이글형) 중 더 앞선 것.
                    cand = []
                    p_paren = seg.find(')')
                    if 0 <= p_paren < 80:            # 제목 끝 괄호(길지 않은 것만)
                        cand.append(p_paren + 1)      # ')' 포함
                    md = re.search(r"['\"]?\d{2,4}\s?[.\-]\s?\d{1,2}\s?[.\-]", seg)
                    if md and md.start() > 8:         # 본문 시작 날짜
                        cand.append(md.start())
                    if cand:
                        pos = min(cand)
                        title, body = seg[:pos].strip(), seg[pos:].strip()
                        if not body:
                            title = seg
                    else:
                        title = seg
        if title:
            t = re.sub(r'\s+', ' ', title).strip()
            # 제목 뒤에 본문 번호섹션('1. 법원 결정'…)이 붙어 크램되면 첫 섹션에서 절단
            t = re.split(r'\s[1-9]\.\s', t, maxsplit=1)[0].strip()
            lines.append(f'📋 {_trunc_clean(t, 100)}')
        if body:
            body = re.sub(r'\s+', ' ', body).strip()
            _tb = f"{title or ''} {body}"
            _appeal = False   # 이의신청 기한 표시 여부 (상폐 해당/결정 단계에서만)
            # 핵심 결과 상단 요약. ※ 순서 중요 — 결정된 것 먼저, 진행중(예정)은 뒤.
            #   불복(효력정지 가처분) → 상폐기준 해당(결정) → 개선기간 종료(심사 예정)
            #   → 개선기간 부여 → 심의대상 → 상폐 결정
            if any(k in _tb for k in ('효력정지', '가처분', '집행정지')):
                # 가처분은 신청/인용/기각 구분 필수 — '기각'인데 '불복 신청'으로
                # 표기하면 보유자가 상폐 정지로 오해(실제는 불복 실패→정리매매 재개).
                if '기각' in _tb or '각하' in _tb:
                    _m = '🚨 결과: 효력정지 가처분 기각'
                    if '재개' in _tb or '정리매매' in _tb:
                        _m += ' — 상장폐지 절차 재개'
                    lines.append(_m)
                    mtm = re.search(r"정리매매\s*\(?\s*('?[\d.]+\s*~\s*'?[\d.]+)", _tb)
                    if mtm:
                        lines.append(f'🕐 정리매매: {mtm.group(1).strip()}')
                elif '인용' in _tb:
                    lines.append('🛡 결과: 효력정지 가처분 인용 — 상장폐지 절차 정지')
                else:
                    lines.append('🛡 상장폐지 불복 — 효력정지 가처분 신청')
            elif '상장폐지기준에 해당' in body:
                lines.append('🚨 결과: 상장폐지기준 해당 (이의신청 가능)')
                _appeal = True
            elif '개선기간' in _tb and '종료' in _tb and '예정' in body:
                # 개선기간 종료 — 상폐 여부 아직 미결정(심사 예정)
                lines.append('⏳ 결과: 개선기간 종료 — 상장폐지 여부 심사 예정')
            elif '개선기간' in _tb and '부여' in body:
                lines.append('🚨 결과: 개선기간 부여')
            elif '심의대상' in body:
                lines.append('🚨 결과: 실질심사 대상 결정')
            elif '상장폐지' in body and ('결정하' in body or '확정' in body):
                lines.append('🚨 결과: 상장폐지 결정')
                _appeal = True
            # 이의신청 기한 — 상폐 해당/결정 단계에서만(개선기간 종료 등은 조건부·미래라 제외)
            if _appeal:
                ma = re.search(r'(\d+)\s*일\s*\(?\s*영업일', body)
                if ma and '이의신청' in body:
                    lines.append(f'📅 이의신청 기한: {ma.group(1)}영업일')
            # 본문 문장별 분리 (통짜 500자 → 스캔 가능)
            for s in re.split(r'(?<=[다요][.)])\s+', body):
                s = s.strip()
                if len(s) >= 8:
                    lines.append(f'  • {_trunc_clean(s, 180)}')
                if len(lines) >= 7:
                    break

    return lines


def parse_unfaithful_disclosure(kv: dict) -> list:
    """불성실공시법인 지정예고 / 지정 / 미지정 — 유형·사유·벌점·제재금·심사사유.

    3변형 통합. 구(舊) 폴백은 원시 번호('2. 3. 4. 5.')와 매 건 동일한 법적
    boilerplate(8점↑ 거래정지 등)를 그대로 노출 → 핵심(벌점·제재금·실질심사)이 묻힘.
    벌점 라벨은 서식별로 다름: 지정=부과벌점 현황(+누계), 예고=최근 1년간 누계.
    """
    lines = []

    # 유형 키는 서식별로 '불성실공시 유형'(유가·예고) / '유형'(코스닥 지정)
    if v := _get(kv, '불성실공시 유형', '불성실공시유형', '유형'):
        lines.append(f'📋 유형: {_trunc(v, 30)}')

    if v := _get(kv, '불성실공시 내용', '내용'):
        body = _trunc_clean(re.sub(r'\s+', ' ', v), 150)
        lines.append(f'📄 사유: {body}')

    # 미지정(지정유예 등) — 결과가 곧 핵심
    if v := _get(kv, '미지정 사유'):
        lines.append(f'✅ 결과: {_trunc(v, 50)}')

    # 지연 정도 — 사유발생 → 실제 공시 간격이 위반 심각도를 보여줌
    occurred = _get(kv, '사유발생일', '원공시일')
    disclosed = _get(kv, '공시일')
    if occurred and disclosed and occurred != disclosed:
        lines.append(f'📅 사유발생 {occurred} → 공시 {disclosed}')

    # 벌점 — 서식 3종: 유가 지정('부과벌점 현황'+'누계벌점'), 코스닥 지정('부과벌점'
    # 단독키 + '최근 1년간…'), 예고(누계만). 당해/누계를 혼동하지 않도록 분리 추출.
    # ※ 누계 10점↑ 관리종목·8점↑ 거래정지 사유라 정확한 구분이 중요.
    own = None
    if v := _get(kv, '부과벌점 현황'):          # 유가 지정: 값이 '부과벌점 N'
        if m := re.search(r'([\d.]+)', v):
            own = m.group(1)
    if own is None:                              # 코스닥 지정: 정확히 '부과벌점' 키
        for k, v in kv.items():
            if (re.sub(r'^\d+\.\s*', '', k).strip() == '부과벌점'
                    and v and (m := re.search(r'([\d.]+)', v))):
                own = m.group(1)
                break
    cum = None
    for cand in ('누계벌점(환산적용벌점)', '누계벌점',
                 '최근 1년간 불성실공시법인 부과벌점'):
        if v := _get(kv, cand):
            if m := re.search(r'([\d.]+)', v):
                cum = m.group(1)
                break
    if own and cum:
        lines.append(f'📊 부과벌점: {own}점 (누계 {cum}점)')
    elif own:
        lines.append(f'📊 부과벌점: {own}점')
    elif cum:
        lines.append(f'📊 최근1년 누계벌점: {cum}점')

    if v := _get(kv, '공시위반제재금(원)', '공시위반제재금'):
        lines.append(f'💸 제재금: {_fmt_amount(v)}원')

    # 상장적격성 실질심사 — '해당'이면 상폐 심사 트리거라 최우선 경고 (미해당은 생략)
    if v := _get(kv, '상장적격성 실질심사사유 발생 여부'):
        if '미해당' not in v:
            lines.append(f'🚨 상장적격성 실질심사 사유: {_trunc(v, 30)}')

    if v := _get(kv, '지정여부 결정시한', '결정시한'):
        lines.append(f'📅 결정시한: {v}')
    if v := _get(kv, '지정ㆍ부과일자', '지정일자', '지정일'):
        lines.append(f'📅 지정일: {v}')
    if v := _get(kv, '결정일'):
        lines.append(f'📅 결정일: {v}')

    return lines


def parse_market_notice(kv: dict) -> list:
    """KRX 기타시장안내(관리종목 지정우려) — 주가·시총·거래량·매출액 미달 산문 안내.

    표 없는 산문 문서(KV 비어있음)라 범용 폴백이 '제목 : (주)…'의 첫 괄호
    '(주)'에서 문장을 쪼개 헤더/불릿을 깨뜨림. 원문에서 안내유형·사유(헤더)와
    본문 문장을 복원. 회사명·제목구(사유 중복)는 제거하고 문장 단위로만 분리.
    (_PARSER_MAP은 '관리종목지정우려'로만 라우팅 — 상장폐지·상장공시위원회 결과
    등 '1. 제목' 구조는 parse_market_measure가 더 정확히 처리하므로 제외.)"""
    html = kv.get('_html', '') or ''
    html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.S | re.I)
    txt = re.sub(r'<[^>]+>', ' ', html)
    txt = re.sub(r'\s+', ' ', txt).strip()
    if not txt:
        return []

    # 헤더 — '기타시장안내(유형)(사유)' 프리픽스에서 유형·사유 추출.
    # 프리픽스에 '기타시장안내(유형)'가 여러 번 나오므로 사유 괄호가 붙은 매치를 우선.
    mh = None
    for m in re.finditer(r'기타시장안내\s*\(([^)]+)\)(?:\s*\(([^)]+)\))?', txt):
        mh = m
        if m.group(2):
            break
    typ = mh.group(1).strip() if mh else '시장안내'
    reason = (mh.group(2) or '').strip() if mh else ''
    typ = re.sub(r'종목$', '', typ)          # 분류 접미 '…종목' 제거
    typ = re.sub(r'(지정)', r' \1', typ).strip() or '시장안내'
    header = f'⚠️ [기타시장안내] {typ}'
    if reason:
        header += f' ({reason})'
    lines = [header]

    # 본문 — '제목 :' 이후에서 선두 회사명·제목구(사유 중복) 제거 후 규정/설명 절부터.
    # 제목·규정 앵커를 모두 못 찾으면(구조 상이) 프리픽스 노이즈를 뱉지 않도록 폴백에 위임.
    mb = re.search(r'제목\s*:\s*(.*)', txt)
    m2 = re.search(r'((?:유가증권|코스닥|코넥스)[^,]{0,8}상장규정.*)', txt)
    if not mb and not m2:
        return []
    body = (mb.group(1) if mb else txt).strip()
    body = re.sub(r'^\(주\)\S+\s*', '', body)                  # 선두 회사명
    m2 = re.search(r'((?:유가증권|코스닥|코넥스)[^,]{0,8}상장규정.*)', body)
    if m2:
        body = m2.group(1)                                     # 제목구 뒤 본문부터
    # 문장 분리('…다. ' 경계) — 괄호·(주)에서는 쪼개지 않음
    for s in re.split(r'(?<=다\.)\s+', body)[:4]:
        s = s.strip()
        if len(s) > 4:
            lines.append(f'  • {_trunc_clean(s, 220)}')
    return lines if len(lines) > 1 else []


def parse_rehabilitation(kv: dict) -> list:
    """회생절차 개시신청/개시결정/계획인가 등 — 관할법원·신청사유·신청일·향후대책.

    회생절차 주요사항보고서는 국/영문 이중언어 서식이라 범용 폴백이 양쪽 필드를
    중복 덤프하고 정정표 헤더셀(항목/정정전/후)까지 노출. 영문 키(Competent court/
    Reasons/Date/Actions)가 값 정렬이 안정적이라 이를 기준으로 추출."""
    rnm = kv.get('_report_nm', '')
    m = re.search(r'회생절차\s*([가-힣]*)', rnm)
    sub = (m.group(1) if m else '') or '개시신청'
    lines = [f'⚖️ 회생절차 {sub}']

    if v := _get(kv, '2. Competent court', 'Competent court', '관할법원'):
        lines.append(f'🏛 관할법원: {v}')
    if v := _get(kv, '3. Reasons for application', 'Reasons for application',
                 '신청의 사유', '신청사유'):
        lines.append(f'📋 신청사유: {_trunc_clean(v, 90)}')
    if v := _get(kv, '4. Date of application', 'Date of application', '신청일자', '신청일'):
        lines.append(f'📅 신청일: {v}')

    # 향후대책 — 국문 요약('5. 향후대책 및 일정', 간결) 우선, 장문/부재면 영문 Actions
    plan = _get(kv, '5. 향후대책 및 일정', '향후대책 및 일정')
    if not plan or len(plan) > 120:
        plan = plan or _get(kv, '5. Actions to be taken and schedule',
                            'Actions to be taken and schedule')
    if plan:
        plan = re.sub(r'^-\s*', '', re.sub(r'\s+', ' ', plan)).strip()
        lines.append(f'📋 향후대책: {_trunc_clean(plan, 150)}')

    return lines if len(lines) > 1 else []
