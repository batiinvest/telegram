"""
dart_parsers_status.py — 거래정지·신탁·제재·정정 관련 공시 파서
(2026-07 dart_parsers 분할 — 파서 원문 무변경 이식)
"""
import re  # noqa: F401
from dart_parse_helpers import *  # noqa: F401,F403  헬퍼·상수·_get/_trunc·log


def parse_trading_halt(kv: dict) -> list:
    """주권매매거래정지 / 기간변경 / 해제"""
    lines = []

    # ── KRX 매매거래정지및정지해제(중요내용공시) 형식 (번호 키) ──
    krx_type = _get(kv, '2. 매매거래정지 유형', '매매거래정지 유형')
    krx_reason = _get(kv, '5. 매매거래정지 사유', '매매거래정지 사유')
    if krx_type or krx_reason:
        lines.append('🔒 매매거래정지' + (f' — {krx_type}' if krx_type else ''))
        if krx_reason:
            lines.append(f'📋 사유: {_trunc(krx_reason, 70)}')
        if v := _get(kv, '3. 매매거래정지 일시', '매매거래정지 일시'):
            lines.append(f'🕐 정지: {v}')
        if (v := _get(kv, '4. 매매거래정지 해제일시', '매매거래정지 해제일시')) and v.strip() not in ('-', ''):
            lines.append(f'🔓 해제: {v}')
        return lines

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
        def _period(label: str, v: str):
            m = re.match(r'^(.*?~)\s*(.*)$', v)
            head, rest = (m.group(1).strip(), m.group(2).strip()) if m else (v, '')
            b = _numbered_with_lead(rest, max_items=6, val_limit=300) if rest else []
            if rest and not b and len(rest) <= 60:          # 짧은 조건은 한 줄('…~ 2026년 10월 01일')
                return [f'  {label}: {_trunc_clean(head + " " + rest, 180)}'], rest
            out = [f'  {label}: {_trunc_clean(head, 120)}']
            if rest:
                out += [f'  {x}' for x in b] if b else [f'      {_trunc_clean(rest, 300)}']
            return out, rest

        bl, brest = _period('변경전', before) if before else ([], '')
        al, arest = _period('변경후', after) if after else ([], '')
        if brest and brest == arest:     # 해제조건 동일 — 변경전은 기간만
            bl = bl[:1]
        lines.extend(bl + al)
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
    # 금감원 서식(주요사항보고서·증권신고서 등: '정정대상 공시서류 :' + [항목|정정사유|정정전|정정후] 표)
    # — 아래 거래소 서식 로직은 이 표를 못 읽어 헤더 없이 본문만 나가던 문제(무엇이 정정됐는지 미표시)
    if any('정정대상 공시서류' in k for k in kv):
        if fss := _fss_amendment(kv):
            return fss

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
            lines.append(f'📋 사유: {_trunc_clean(v, 200)}')

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
        # old=필드 라벨성 텍스트, new=빈값 → 중첩표 헤더 행 (의미 있는 변경 아님).
        # 'new가 빈값'일 때만 적용하므로 실제 값→값 변경은 가려지지 않는다.
        # 예: '결정내용: 제1회 관계인집회기일 → -' (세븐브로이 2026-09-18).
        # '종료일: 2026-06-30 → -'(값 삭제)는 old가 날짜라 아래 조건에서 제외됨.
        if (new_v.strip() in ('-', '', '해당없음', '없음')
                and re.search(r'[가-힣]', old_v) and len(old_v) <= 20
                and not re.search(r'\d{4}-\d{2}-\d{2}|\d[\d,]{2,}|\d+(?:\.\d+)?\s*%', old_v)):
            return True
        return False

    # 'old → new' 한 줄이 잘릴 한글 값 — 바뀐 부분이 '…' 뒤로 숨음(비에이치 '연장 만기일 :
    # 2026-… → 2028-…', 담보 채권자 KB증권→한국증권금융 등) → 바뀐 항목/문장만 [전]/[후] 줄로.
    # 판정 = 실제 한 줄 포맷(_fmt_amendment_val: 60자 절단 후 일반 값은 40자)이 잘리는지 —
    # 금액·비율·날짜처럼 깔끔히 포맷되는 값은 기존 한 줄 유지.
    def _long(field: str, o: str, n: str) -> bool:
        if not re.search(r'[가-힣]{2}', o + ' ' + n):
            return False
        if any('…' in _fmt_amendment_val(field, _trunc(x, _MAX_VAL_LEN)) for x in (o, n)):
            return True
        # 60자 초과 서술 — 날짜 포맷(_clean_date)이 긴 문장을 날짜 조각으로 뭉개 '…' 없이 잘리는
        # 경우(디에스케이 '-거래종결일')도 포함. 금액·비율 필드는 포맷 한 줄이 더 읽기 좋아 제외.
        return (max(len(o), len(n)) > _MAX_VAL_LEN
                and not any(k in field for k in ('금액', '가격', '대금', '보증금', '대비', '비율', '%', '비중')))

    # ── 패턴 C: 정정전_* / 정정후_* 접두어 키 비교 (가장 신뢰도 높음) ──────
    sub_seen = {}      # 하위 항목 정정 (head, 항목, 구, 신) → (change_lines 위치, [순번])
    before_keys = {k[4:]: v for k, v in kv.items() if k.startswith('정정전')}
    after_keys  = {k[4:]: v for k, v in kv.items() if k.startswith('정정후')}
    for field, old_v in before_keys.items():
        if len(change_lines) >= _MAX_CHANGES:
            break
        if _clean_amendment_field(field) in _SKIP_FIELDS or re.search(r'참석|불참', field):
            continue   # 사외이사 참석 인원('🔧 참석: 3 → 4')은 정정 핵심이 아님
        new_v = after_keys.get(field, '')
        # 하위 항목 여럿인 행 — 바뀐 하위 항목만 '🔧 순번2 (주)상상인저축은행 담보제공기간종료일: 구 → 신'
        so, sn = _sub_parts('정정전_' + field, _ws(old_v)), _sub_parts('정정후_' + field, _ws(new_v))
        if so and sn and [l for l, _ in so] == [l for l, _ in sn]:
            head, seq = _clean_amendment_field(field.split(' - ')[0]), ''
            if m_ := re.search(r'순번\s*(\d+)\.?\s*(.*)', head):
                seq, head = m_.group(1), m_.group(2).strip()
            for (lab, o), (_, n) in zip(so, sn):
                if o == n:
                    continue
                key = (head, lab, o, n)
                if key in sub_seen:              # 순번만 다른 같은 변경 → '순번2·3' 한 줄
                    i_, seqs = sub_seen[key]
                    seqs.append(seq)
                elif len(change_lines) < _MAX_CHANGES:
                    sub_seen[key] = (len(change_lines), [seq] if seq else [])
                    change_lines.append(key)
            continue
        old_c = re.sub(r'\s+', ' ', old_v).strip()
        new_c = re.sub(r'\s+', ' ', new_v).strip()
        if old_c and new_c and old_c != new_c and not _is_header_row(field, old_c, new_c):
            # 긴 서술형(주요내용 등) — 양쪽 60자 절단 한 줄로는 무엇이 바뀌었는지 안 보임.
            # 바뀐 항목/문장만 [전]/[후] 줄로(정정후가 조각일 때의 전문 비교는 _prose_diff).
            if _long(field, old_c, new_c):
                full = re.sub(r'\s+', ' ', kv.get(field, '') or '').strip()
                if pd := _prose_diff(_clean_amendment_field(field), old_c, new_c, full):
                    change_lines.append(pd)
                continue
            old_fmt = _fmt_amendment_val(field, _trunc(old_c, _MAX_VAL_LEN))
            new_fmt = _fmt_amendment_val(field, _trunc(new_c, _MAX_VAL_LEN))
            change_lines.append(f'🔧 {_clean_amendment_field(field)}: {old_fmt} → {new_fmt}')

    for key, (i_, seqs) in sub_seen.items():   # 하위 항목 변경 줄 완성(순번 묶음·금액 포맷)
        head, lab, o, n = key
        tag = (f'순번{"·".join(dict.fromkeys(seqs))} ' if seqs else '') + head
        change_lines[i_] = (f'🔧 {tag} {lab}'.replace('  ', ' ') + f': {_fmt_amendment_val(lab, o) if o else "-"}'
                            f' → {_fmt_amendment_val(lab, n) if n else "-"}')
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
                if not _is_header_row(field_name, old_clean, new_clean) and _long(field_name, old_clean, new_clean):
                    if pd := _prose_diff(_clean_amendment_field(field_name), old_clean, new_clean):
                        change_lines.append(pd)
                elif not _is_header_row(field_name, old_clean, new_clean):
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
                    if old_v != new_v and not _is_header_row(fname, old_v, new_v) and _long(fname, old_v, new_v):
                        if pd := _prose_diff(_trunc(fname, 25), old_v, new_v):
                            change_lines.append(pd)
                    elif old_v != new_v and not _is_header_row(fname, old_v, new_v):
                        old_fmt = _fmt_amendment_val(fname, _trunc(old_v, _MAX_VAL_LEN))
                        new_fmt = _fmt_amendment_val(fname, _trunc(new_v, _MAX_VAL_LEN))
                        change_lines.append(f'🔧 {_trunc(fname, 25)}: {old_fmt} → {new_fmt}')
                j += 1
            i = j
            continue

        break  # 정정 섹션 끝

    lines.extend(change_lines)
    return lines


_FSS_REF = re.compile(r'[<(（]?\s*주?\s*\d+(?:-\d+)?\s*[>)）]\s*(?:정정\s*[전후]|참조)?'
                      r'|정정\s*[전후]\s*[<(（]?\s*주?\s*\d+(?:-\d+)?\s*[>)）]?'
                      r'|\[?\s*변경\s*[전후]\s*\]?|주\s*\d+(?:-\d+)?')


def _fss_is_ref(v: str) -> bool:
    """정정전/후 칸이 본문 주석 참조('<주1> 정정 전', '1) 정정 후', '주2) 참조')인지."""
    v = (v or '').strip()
    return bool(v) and (bool(_FSS_REF.fullmatch(v)) or (len(v) <= 20 and v.endswith('참조')))


def _fss_ref_texts(full: str, start: int, ref_b: str, ref_a: str, doc: str = ''):
    """표 뒤 본문에서 '1) 정정 전 …(구) 1) 정정 후 …(신) 2) 정정 전'의 구·신 텍스트. 없으면 None."""
    def lab(r):
        return re.compile(r'[\s\-:：]*'.join(re.escape(ch) for ch in re.sub(r'\s+', '', r)))
    # 표 안의 참조 셀('(주1) 정정 전 | (주1) 정정 후')은 사이 글이 없음 — 본문 주석이 나올 때까지 다음 위치로
    for _ in range(4):
        mb = lab(ref_b).search(full, start)
        if not mb:
            return None
        ma = lab(ref_a).search(full, mb.end())
        if not ma:
            return None
        if ma.start() - mb.end() >= 5:
            break
        start = ma.end()
    else:
        return None
    nxt = re.compile(r'(?:[<(（]\s*주\s*\d+(?:-\d+)?\s*[>)）]|주\s*\d+(?:-\d+)?\)|(?<![\d.])\d{1,2}\))\s*정정\s*전'
                     r'|정정\s*전\s*[<(（]?\s*주?\s*\d+')
    mn = nxt.search(full, ma.end())
    # 마지막 주석은 다음 참조가 없어 본문 서식까지 이어짐 — 서식 머리말 또는 구 문구 길이 비례로 끝냄
    md = re.compile(r'주요사항보고서\s*/\s*거래소|금융위원회\s*(?:/\s*한국거래소\s*)?귀중|금융감독원장\s*귀하'
                    ).search(full, ma.end())
    cap = ma.end() + int((ma.start() - mb.end()) * 1.6) + 300
    ends = [mn.start() if mn else len(full), md.start() if md else len(full), cap, len(full)]
    if doc:      # 본 서류 제목('주식매수선택권 부여에 관한 신고')이 나오면 본문 시작
        key = re.sub(r'\s+', '', re.sub(r'\(.*$', '', doc))[:8]
        if len(key) >= 4:
            mt = re.compile(r'\s*'.join(map(re.escape, key))).search(full, ma.end())
            if mt:
                ends.append(mt.start())
    end = min(ends)
    strip_lab = lambda x: re.sub(r'^[\s:：\-]*정정\s*[전후][\s:：\-]*', '', x).strip()   # 남은 ': 정정전-' 라벨
    old, new = strip_lab(full[mb.end():ma.start()]), strip_lab(full[ma.end():end])
    return (old, new) if old or new else None


def _table_grid(tbl) -> list:
    """HTML 표 → 2차원 격자(rowspan·colspan 반영, 병합 칸은 같은 텍스트 반복). 중첩 표 제외."""
    grid, pending = [], {}
    for tr in [tr for tr in tbl.find_all('tr') if tr.find_parent('table') is tbl]:
        cells = tr.find_all(['td', 'th'], recursive=False)
        out, col, k = {}, 0, 0
        while True:
            if col in pending:
                rem, txt = pending[col]
                out[col] = txt
                if rem <= 1:
                    del pending[col]
                else:
                    pending[col] = (rem - 1, txt)
                col += 1
                continue
            if k < len(cells):
                c = cells[k]
                k += 1
                txt = re.sub(r'\s+', ' ', c.get_text(' ')).strip()
                try:
                    cs = max(1, int(c.get('colspan') or 1))
                    rs = max(1, int(c.get('rowspan') or 1))
                except ValueError:
                    cs = rs = 1
                for j in range(cs):
                    out[col + j] = txt
                    if rs > 1:
                        pending[col + j] = (rs - 1, txt)
                col += cs
                continue
            if any(c_ > col for c_ in pending):
                col += 1
                continue
            break
        grid.append([out.get(i, '') for i in range(max(out) + 1)] if out else [])
    return grid


def _fss_amendment(kv: dict) -> list:
    """금감원 서식 [기재정정] 헤더 — 📄 대상 서류(최초 제출일) · 📋 사유 · 🔧 항목별 변경.

    정정표 [항목|정정사유|정정전|정정후]는 같은 사유가 rowspan으로 병합돼 이어지는 행의 칸
    수가 1 적고, 주석 참조('1) 정정 전')는 표 아래 본문에 구·신 문구가 따로 있다 — colspan으로
    칸을 정렬하고 참조는 본문에서 찾아 바뀐 문장만 [전]/[후]로."""
    from bs4 import BeautifulSoup
    lines = []
    doc = next((v for k, v in kv.items() if '정정대상 공시서류' in k and '최초' not in k and v), None)
    date = next((v for k, v in kv.items() if '최초제출일' in k and v), None)
    if date and (md := re.search(r'(\d{4})\s*[년.\-]\s*(\d{1,2})\s*[월.\-]\s*(\d{1,2})', date)):
        date = f'{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}'
    if doc:
        doc = re.sub(r'[:：]\s*$', '', doc.strip())
        lines.append(f'📄 {_trunc_clean(doc, 80)}' + (f' (최초 {date})' if date else ''))

    raw = kv.get('_html', '')
    if not raw:
        return lines
    soup = BeautifulSoup(raw, 'html.parser')
    tbl = None
    for tb in soup.find_all('table'):
        rows_ = [tr for tr in tb.find_all('tr') if tr.find_parent('table') is tb]
        if rows_:
            h0 = re.sub(r'\s+', '', rows_[0].get_text(' '))
            if '항목' in h0 and '정정전' in h0 and '정정후' in h0:
                tbl = tb
                break
    if not tbl:
        return lines
    grid = _table_grid(tbl)
    hdr = [re.sub(r'\s+', '', c) for c in grid[0]]
    it_cols = [i for i, h in enumerate(hdr) if h == '항목']
    i_b = next((i for i, h in enumerate(hdr) if h == '정정전'), None)
    i_a = next((i for i, h in enumerate(hdr) if h == '정정후'), None)
    i_rs = next((i for i, h in enumerate(hdr) if '정정사유' in h), None)
    if not it_cols or i_b is None or i_a is None:
        return lines

    full = re.sub(r'\s+', ' ', soup.get_text(' '))
    ttxt = re.sub(r'\s+', ' ', tbl.get_text(' ')).strip()
    p = full.find(ttxt[-80:]) if ttxt else -1
    after_tbl = p + 80 if p >= 0 else 0

    entries, reasons, section = [], [], ''
    need = max(it_cols + [i_b, i_a] + ([i_rs] if i_rs is not None else []))
    for row in grid[1:]:
        if row and len(set(row)) == 1:                       # 섹션 머리행(전폭 병합)
            section = re.sub(r'^\s*\d{1,2}\.\s*', '', row[0])[:30]
            continue
        if len(row) <= need:
            continue
        item = ' '.join(dict.fromkeys(x for x in (row[i] for i in it_cols) if x))
        row = list(row)
        row[i_b], row[i_a] = _unglue(row[i_b]), _unglue(row[i_a])   # 하위 표가 글자만 이어 붙은 셀
        if len(re.sub(r'[\s.]', '', item)) <= 2 and section:
            item = f'{section} {item}'
        b, a = row[i_b], row[i_a]
        rs = row[i_rs] if i_rs is not None else ''
        if rs and rs not in reasons and rs not in ('-', ''):
            reasons.append(rs)
        if not item or (b == a and not _fss_is_ref(b)):
            continue
        entries.append((re.sub(r'^\s*\d{1,2}\.\s*', '', item), b, a))

    if reasons:
        shown = ' · '.join(_trunc_clean(r, 60) for r in reasons[:2])
        lines.append(f'📋 사유: {shown}' + (f' 외 {len(reasons) - 2}건' if len(reasons) > 2 else ''))

    shown = 0
    for item, b, a in entries[:6]:
        it = _trunc_clean(item, 40)
        bb, aa = b.strip(), a.strip()
        if _fss_is_ref(bb) or _fss_is_ref(aa):
            got = _fss_ref_texts(full, after_tbl, bb, aa, doc or '') if bb != aa else None
            pd = _prose_diff(it, got[0], got[1], max_seg=3, seg_len=200) if got else ''
            lines.append(pd or f'🔧 {it}: 세부 내용 원문 참조({_trunc(aa or bb, 20)})')
        elif bb in ('-', '') and aa not in ('-', ''):
            if '첨부' in aa:
                lines.append(f'🔧 {it}: 첨부 추가')
            else:
                lines.append(f'🔧 {it}: 내용 추가 — {_trunc_clean(aa, 200)}')
        elif aa in ('-', '') and bb not in ('-', ''):
            lines.append(f'🔧 {it}: 삭제 — {_trunc_clean(bb, 150)}')
        elif max(len(bb), len(aa)) <= 60:
            lines.append(f'🔧 {it}: {bb} → {aa}')
        else:
            lines.append(_prose_diff(it, bb, aa, max_seg=3, seg_len=200)
                         or f'🔧 {it}: {_trunc_clean(bb, 80)} → {_trunc_clean(aa, 80)}')
        shown += 1
        if sum(len(x) for x in lines) > 2200:      # 대형 신고서 정정(수십 항목·표 덤프) 길이 상한
            break
    if len(entries) > shown:
        lines.append(f'🔧 …외 {len(entries) - shown}개 항목')
    return lines


def parse_misc_mgmt(kv: dict) -> list:
    """기타주요경영사항(자율공시) — 주요내용이 곧 공시의 본체.

    양식 두 가지: 1.제출사유/2.주요내용/3.결정(발생)일자/4.기타 투자판단에 참고할 사항,
    1.제목/2.주요내용/3.결정(확인)일자/4.기타 투자판단과 관련한 중요사항/※관련공시.
    제출사유·제목은 공시 제목 괄호에 이미 노출되므로 생략, 주요내용을 넉넉히 표시.
    4.기타엔 철회사유·계약금 몰취·FDA 허가일 등 핵심이 자주 담겨 관련공시 목록·
    '결정일자는 이사회 결의일' 상투문만 떼고 표시.
    """
    lines = []

    # [기재정정]이면 정정후 값은 바뀐 문장 조각일 수 있음 → 정정 반영 전문 우선
    body = _get_body(kv, '2. 주요내용', '주요내용') or ''
    stripped = _strip_disclaimer(body).strip()
    if stripped:
        bullets = _parse_numbered_body(stripped)
        if bullets and len(bullets) >= 2:
            lines.extend(bullets)
        else:
            clean = re.sub(r'^[\-·•]\s*', '', _ws(stripped)).strip()
            # 주요내용이 공시 본체 → 사실상 전문 표시 (2000자 초과 극단 케이스만 절단,
            # 4000자 초과 발송은 managers._split_text가 분할 처리)
            lines.append(f'📋 {_trunc_clean(clean, 2000)}')

    if v := _get(kv, '결정(발생)일자', '결정(확인)일자', '결정일자', '발생일자', '확인일자'):
        lines.append(f'📅 결정일: {v}')

    etc = re.sub(r'\s+', ' ', _get(kv, '4. 기타 투자판단', '기타 투자판단') or '').strip()
    rel_txt = _get(kv, '관련공시', '관련 공시') or ''
    if m := _REL_MARK.search(etc):
        rel_txt, etc = etc[m.start():] + ' ' + rel_txt, etc[:m.start()]
    elif re.match(r'\d{4}[.-]\d{2}[.-]\d{2}', etc):     # 값 전체가 관련공시 목록
        rel_txt, etc = etc + ' ' + rel_txt, ''
    notes = [n for n in _etc_segments(etc) if not _ETC_BOILER.search(n)]
    if len(notes) == 1:
        lines.append(f'📎 참고: {_trunc_clean(notes[0], 400)}')
    elif notes:
        lines.append('📎 참고:')
        lines.extend(f'  • {_trunc_clean(n, 250)}' for n in notes[:5])

    if rel := _related_list(rel_txt):
        lines.append(f'🔗 관련: {rel}')

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

    # KRX 기타시장안내(1.제목/2.내용) 형은 아래 프로즈 경로가 더 정확(가처분 기각 등
    # 결과 판정 포함)하므로, 약한 '기타' 불릿으로 lines를 선점해 폴백을 막지 않도록 제외.
    if not (_get(kv, '제목') and _get(kv, '내용')) and (v := _get(kv, '5.기타', '기타')):
        lines.extend(_parse_etc_field(v)[:4])

    # KRX 기타시장안내형 — 정형 필드가 없으면 제목/내용 KV, 그마저 없으면(표 없는 산문)
    # 원문 '제목 :' 이후를 공시명 괄호 제목 기준으로 제목/본문 분리(_mkt_title_body).
    # 결과 라벨은 _mkt_verdict — 제목 신호 우선, 없으면 본문의 '확정' 문장만(…한 바 있/…경우
    # 문장 제외). 예전엔 본문 아무 데서나 단어를 잡아 '우려' 공지에 '상장폐지 결정',
    # 실질심사 대상 결정에 '개선기간 종료', 상장폐지 의결에 '개선기간 부여'가 붙었음(10-05 감사).
    if not lines:
        title = _get(kv, '제목')
        body = _get(kv, '내용')
        if not title and not body:
            title, body = _mkt_title_body(kv)
        title = re.sub(r'\s+', ' ', title or '').strip()
        body = _ws(body)
        if title:
            lines.append(f'📋 {_trunc_clean(title, 150)}')
        # 결과 판정엔 공시명 괄호 제목도 함께 — 표형(1.제목/2.내용)은 제목에 '(상장폐지 기준 해당)'이 없음
        _nm = re.sub(r'^\[[^\]]+\]', '', kv.get('_report_nm', ''))
        lines.extend(_mkt_verdict(f'{title} {_nm}', body))
        # 본문 원문 줄(<br>) → 문장별 분리. 원문 목록 줄('- 경과일수 : 51일')은 앞 문장 아래 하위 줄
        # (예전엔 '- 경과일수 : 51일 - 20억원 미만 일수 : 51일 - …'이 한 불릿으로 붙었음, 소프트센)
        n0 = len(lines)
        for para in body.split(_BR):
            para = para.strip()
            if not para:
                continue
            if re.fullmatch(r'\((?:주식회사\s*)?한국거래소[^()]*\)|\([^()]*시장본부\)', para):
                continue                         # 끝 서명 '(한국거래소)'
            if len(lines) > n0 and re.fullmatch(r'\([^()]{2,60}\)', para):
                lines[-1] += f' {para}'          # '(코스닥시장 상장규정 제81조)'·'(해제요건 : 10일 이상)'은 앞 줄 꼬리
            elif len(lines) > n0 and len(para) <= 200 and _MKT_SUB.match(para):
                lines.append(f'    {para}')
            else:
                for s_ in re.split(r'(?<=[다요][.)])\s+', para):
                    s_ = s_.strip()
                    if len(s_) >= 8 or re.fullmatch(r'[\[<【].{1,15}[\]>】]', s_):   # '[신청취지]' 머리 유지
                        lines.append(f'  • {_trunc_clean(s_, 400)}')
                    if len(lines) >= 18:
                        break
            if len(lines) >= 18:   # 12→18: 원문 목록 줄을 하위 줄로 펼친 만큼 — 2번째 섹션까지 유지
                break

    return lines


_MKT_SUB = re.compile(r'[-–·▶○●□■※*]\s*\S|[①-⑳]|[가-하][.)]\s')   # 하위 줄로 내릴 원문 목록 머리
_MKT_DATEP = r"\(\s*['‘’]?\d{2,4}\s?[.\-]\s?\d{1,2}\s?[.\-]\s?\d{1,2}\.?\s*\)"
_MKT_Q = str.maketrans({'‘': "'", '’': "'", '“': '"', '”': '"', 'ㆍ': '·'})


def _ws_find_end(seg: str, pat: str) -> int:
    """공백·따옴표 변형 무시하고 pat을 seg에서 찾아 끝 위치(seg 인덱스) 반환, 없으면 -1."""
    s = seg.translate(_MKT_Q)
    p = re.sub(r'\s+', '', pat.translate(_MKT_Q))
    if not p:
        return -1
    idx = [i for i, ch in enumerate(s) if not ch.isspace()]
    j = ''.join(s[i] for i in idx).find(p)
    return idx[j + len(p) - 1] + 1 if j >= 0 else -1


def _mkt_title_body(kv: dict):
    """표 없는 KRX 산문 공지 → (제목, 본문). 제목 끝 = 공시명 괄호 제목('시가총액 미달에 따른
    상장폐지 우려 관련 안내')이 원문에 나타나는 끝(+닫는 괄호·'(2026.09.29)' 날짜). 예전엔 첫 ')'나
    첫 날짜에서 잘라 '📋 (주)', '…안내(' 제목·'…결정 거래소는' 본문 섞임이 생겼음."""
    import html as _html
    raw = kv.get('_html', '')
    if not raw:
        return None, None
    txt = re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r'(?i)<br\b[^>]*>|</p\s*>', _BR_RAW, txt)    # 원문 줄바꿈 → 본문 목록 줄 구분
    txt = _html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', txt))).strip()
    txt = _ws(re.sub(r'\s*\ue000[\s\ue000]*', _BR, txt))
    m = re.search(r'제\s*목\s*[:：]\s*(.+)$', txt)
    if not m:
        return None, None
    seg = m.group(1).strip()
    nm = re.sub(r'^\[[^\]]+\]', '', kv.get('_report_nm', '')).strip()
    ends = [e for g in re.findall(r'\(((?:[^()]|\([^()]*\))+)\)', nm)
            if len(re.sub(r'\s+', '', g)) >= 4
            for e in [_ws_find_end(seg[:300], g)] if e > 0]
    if ends:
        cut = max(ends)
        m2 = re.match(r'\s*\)?\s*(?:' + _MKT_DATEP + r')?', seg[cut:])
        cut += m2.end()
    elif mk := re.search(r'기타시장안내\s*\([^()]*\)', seg[:200]):
        cut = mk.end()
    else:
        md = re.search(_MKT_DATEP, seg[:200])
        mb = re.search(r"\s(?=(?:한국)?거래소는|동\s?사는|당사는|코스닥시장(?:본부|위원회)는|"
                       r"유가증권시장본부는|['‘]\d{2}\.\d)", seg[:250])
        ms = re.search(r'\s[1-9]\.\s', seg[:250])
        cut = md.end() if md else (mb.start() if mb and mb.start() > 5 else (ms.start() if ms else 0))
    title, body = seg[:cut].strip(), seg[cut:].strip()
    return (title or None), (body or None)


def _mkt_date(y: str, mo: str, d: str) -> str:
    y = ('20' + y) if len(y) == 2 else y
    return f'{y}-{int(mo):02d}-{int(d):02d}'


def _mkt_deadline(text: str, last: bool = False):
    """기한 날짜 — "'26.10.30 限" / "20영업일(2026.10.30.) 이내" / "'26.10.21까지".
    본문 위치 기준 첫 기한(기본) 또는 마지막 기한(last — 연장 공지: "당초 조사기간('26.09.28 限)을
    15일 연장 … '26.10.21까지"에서 새 기한은 뒤쪽)."""
    hits = []
    for pat in (r"['‘’]?(\d{2,4})\.\s?(\d{1,2})\.\s?(\d{1,2})\.?\s*限",
                r"(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})\.?\s*\)\s*(?:이내|까지)",
                r"['‘’](\d{2})\.\s?(\d{1,2})\.\s?(\d{1,2})\.?\s*까지",
                r"(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})\.?\s*까지"):
        hits += [(m.start(), _mkt_date(*m.groups())) for m in re.finditer(pat, text or '')]
    if not hits:
        return None
    return (max(hits) if last else min(hits))[1]


# 결과 공지 본문의 '확정' 문장 패턴(공백 제거 후 비교) — 과거('…한 바 있')·조건('…경우') 문장 제외
_MKT_DECISIONS = (
    ('🚨 결과: 상장폐지 결정',
     r'"?상장폐지"?로(?:심의[·]?)?의결|상장폐지를의결|상장폐지를결정하였|상장폐지가결정되었|'
     r'상장폐지하기로결정|'
     r'상장폐지기준(?:\([^)]*\))?에해당한다고결정|상장폐지기준(?:\([^)]*\))?에해당됨에따라'),
    ('🚨 결과: 개선기간 부여', r'개선기간을?\S{0,25}?부여(?:하기로|하였|하고자|함)'),
    ('⏳ 결과: 상장폐지 여부 심의 속행', r'심의를속행'),
    ('⚠️ 결과: 상장적격성 실질심사 사유 발생 — 심의대상 여부 결정 예정', r'실질심사사유가(?:추가로)?발생하였'),
    ('✅ 결과: 상장 유지', r'상장(?:을)?유지(?:하기로)?(?:결정|의결)하였|상장적격성이인정되었'),
)


def _mkt_body_decision(body: str):
    for s in re.split(r'(?<=다[.)])\s+|(?<=니다)\s+', body or ''):
        # 과거('…한 바 있')·조건('…하는 경우 …')만 제외 — '…정지된 경우로'·'경우에 해당'은 분류 서술이라 유지
        if '바 있' in s or re.search(r"경우(?![\"”’']?(?:로|에\s*해당))", s):
            continue
        sf = re.sub(r'\s+', '', s).translate(_MKT_Q)
        for label, pat in _MKT_DECISIONS:
            if re.search(pat, sf):
                return label
    return None


def _mkt_verdict(title: str, body: str) -> list:
    """시장조치 결과 한 줄(+이의신청·정리매매 일정). 판단 불가면 [] — 오라벨보다 무라벨."""
    t = re.sub(r'\s+', '', title or '').translate(_MKT_Q)
    b = body or ''
    bf = re.sub(r'\s+', '', b).translate(_MKT_Q)
    tb = t + bf
    dl = _mkt_deadline(b, last='연장' in t)
    dls = f' (~{dl})' if dl else ''
    out = []
    resume = False
    if '무효확인' in t and ('기각' in t or '각하' in t):
        out.append('🚨 결과: 상장폐지 무효확인 소송 기각 — 상장폐지 절차 재개')
        resume = True
    elif any(k in tb for k in ('효력정지', '가처분', '집행정지')):
        # 가처분 신청/인용/기각 구분 필수 — '기각'을 '불복 신청'으로 표기하면 보유자가 상폐 정지로 오해
        if '기각' in tb or '각하' in tb:
            resume = '재개' in tb or '정리매매' in tb
            out.append('🚨 결과: 효력정지 가처분 기각' + (' — 상장폐지 절차 재개' if resume else ''))
        elif '인용' in tb:
            out.append('🛡 결과: 효력정지 가처분 인용 — 상장폐지 절차 정지')
        elif '보류' in tb:
            out.append('⏸ 결과: 상장폐지 절차(정리매매) 보류 — 가처분 결정 확인 시까지')
        else:
            out.append('🛡 상장폐지 불복 — 효력정지 가처분 신청')
    elif '우려' in t:
        return []        # 우려·예고는 조건부 경고 — 결과 라벨 없음(제목·본문이 그대로 설명)
    elif '중단' in t and '실질심사' in t:
        out.append('⏹ 결과: 상장적격성 실질심사 절차 중단'
                   + (' — 이미 상장폐지 결정' if '상장폐지결정' in bf else ''))
    elif '조사기간' in t and '연장' in t:
        out.append(f'⏳ 결과: 실질심사 대상 여부 조사기간 연장{dls}')
    elif '개최기한' in t and '연장' in t:
        out.append(f'⏳ 결과: 기업심사위원회 개최기한 연장{dls}')
    elif '기한' in t and ('대상결정' in t or '대상여부' in t):
        out.append(f'⏳ 결과: 실질심사 대상 여부 결정 예정{dls}')
    elif ('실질심사대상' in t or '심의대상' in t) and '결정' in t:
        out.append('🚨 결과: 상장적격성 실질심사(기업심사위원회 심의) 대상 결정'
                   + (f' — 심의 기한 {dl}' if dl else ''))
    elif '실질심사사유' in t:
        out.append('⚠️ 결과: 상장적격성 실질심사 사유 ' + ('추가 ' if '추가' in t else '')
                   + '발생 — 심의대상 여부 결정 예정')
    elif '개선기간종료' in t:
        out.append(f'⏳ 결과: 개선기간 종료 — 상장폐지 여부 심의 예정{dls}')
    elif ('개선계획' in t or '이행내역' in t) and '제출' in t:
        out.append(f'⏳ 결과: 개선계획(이행내역) 제출 — 상장폐지 여부 심의 예정{dls}')
    elif '정리매매' in t and '보류' in t:
        out.append('⏸ 결과: 정리매매 보류')
    elif '상장폐지' in t and '기준' in t and '해당' in t:
        out.append('🚨 결과: 상장폐지기준 해당 — 상장폐지 절차 진행')
    elif '상장폐지사유' in t and '발생' in t and '해소' not in t:
        out.append('⚠️ 결과: 상장폐지 사유 발생')
    else:
        v = _mkt_body_decision(b)
        if v:
            out.append(v)
        elif '상장폐지결정' in t:
            out.append('🚨 결과: 상장폐지 결정')
    if resume:
        mtm = re.search(r"정리매매\s*\(?\s*('?[\d.]+\s*~\s*'?[\d.]+)", b)
        if mtm:
            out.append(f'🕐 정리매매: {mtm.group(1).strip()}')
    # 상장폐지 결정·기준 해당 → 이의신청 기한(날짜 또는 영업일)
    if out and out[0].startswith('🚨 결과: 상장폐지') and '이의신청' in b:
        md = re.search(r'이의신청\s*(?:시한|기한)\s*(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})', b)
        mn = re.search(r'(\d+)\s*일\s*\(?\s*영업일[^)]*\)?\s*이내에\s*이의신청', b)
        if md:
            out.append(f'📅 이의신청 기한: {_mkt_date(*md.groups())}')
        elif mn:
            out.append(f'📅 이의신청 기한: 통보일로부터 {mn.group(1)}영업일')
    # 본 결과와 별개로 실질심사 사유가 추가 발생한 경우(이오플로우 2번 섹션)
    if out and '실질심사' not in out[0] and re.search(r'실질심사사유가?추가로?발생', bf):
        out.append('⚠️ 상장적격성 실질심사 사유 추가 발생')
    return out


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
        b = _numbered_with_lead(plan, max_items=6, val_limit=300) \
            if re.search(r'(?:^|\s)1[.)]', plan) and re.search(r'\s2[.)]', plan) else []
        if len(b) >= 2:
            lines.append('📋 향후대책:')
            lines.extend(b)
        else:
            lines.append(f'📋 향후대책: {_trunc_clean(plan, 400)}')

    return lines if len(lines) > 1 else []


def parse_investee_rehab(kv: dict) -> list:
    """출자법인의 회생절차/파산 신청·사실발생 — 출자법인·신청구분·출자규모·사유.

    투자자(당사)가 출자한 법인의 회생/파산 상태 공시. 신청구분(개시신청/종결/파산
    등)이 핵심 — 종결·인가는 회복, 개시신청·파산은 부실. 한글 번호 키 서식이라
    회사 자신의 회생용 parse_rehabilitation(영문키)과 별개."""
    kind = _get(kv, '3. 신청내역 구분', '신청내역 구분', '신청내역', '구분내용')
    lines = ['🏢 출자법인 회생·파산' + (f' — {kind}' if kind else '')]

    # 출자법인명 — 표 헤더가 '회사명/대표자/…'라 이름 셀의 값이 다음 헤더 '대표자'
    name = next((k for k, v in kv.items()
                 if not k.startswith('_') and (v or '').strip() == '대표자'), None)
    rel = _get(kv, '회사와의 관계')
    biz = _get(kv, '주요사업')
    if name:
        tail = ' · '.join(x for x in (rel, _trunc(biz, 24) if biz else None) if x)
        lines.append(f'🏭 출자법인: {name}' + (f' ({tail})' if tail else ''))

    amt = _get(kv, '출자금액(원)', '출자금액')
    ratio = _get(kv, '자기자본대비(%)')
    if amt and re.search(r'\d', amt):
        lines.append(f'💰 출자금액: {_fmt_amount(amt)}원'
                     + (f' (자기자본대비 {ratio}%)' if ratio else ''))

    if v := _get(kv, '- 신청사유', '신청사유', '사유'):
        v = re.sub(r'^-\s*', '', re.sub(r'\s+', ' ', v)).strip()
        lines.append(f'📋 사유: {_trunc_clean(v, 120)}')

    court = _get(kv, '5. 관할법원', '관할법원')
    date = _get(kv, '4. 신청일자', '신청일자', '신청일', '확인일자')
    seg = [x for x in (court, (f'신청일 {date}' if date else None)) if x]
    if seg:
        lines.append('🏛 ' + ' · '.join(seg))

    return lines if len(lines) > 1 else []


# ══════════════════════════════════════════════════════════════════════
#  2026-10-05 발송분 감사 — 폴백·빈 결과 유형 전용 파서
# ══════════════════════════════════════════════════════════════════════

_INQ_TAIL = re.compile(r'\s*(?:\(\s*공시책임자\s*\)|※\s*(?:본\s*공시는|이\s*내용은|본\s*답변은)).*$')


def _answer_bullets(text: str, limit: int = 8) -> list:
    """답변·해명 본문 → 줄별 불릿. 번호 목록이면 항목별, 아니면 '-'·문장 단위.
    끝의 '※ 본 공시는 …조회공시요구에 대한 답변입니다'·'(공시책임자) …' 상투문 제거."""
    t = _INQ_TAIL.sub('', re.sub(r'\s+', ' ', text or '').strip())
    if not t or t in ('-', '해당사항 없음'):
        return []
    if re.search(r'(?:^|\s)1[.)]', t) and re.search(r'\s2[.)](?!\d)', t):
        b = _numbered_with_lead(t, max_items=limit, val_limit=400)
        if b:
            return b
    segs = []
    for s in _etc_segments(t):
        segs += ([x.strip() for x in re.split(r'(?<=[다음함됨임]\.)\s+', s) if x.strip()]
                 if len(s) > 300 else [s])
    return [f'  • {_trunc_clean(s, 400)}' for s in segs[:limit]]


_ANS_BOILER = re.compile(r'^\s*•\s*본\s*공시는.{0,120}?(?:답변|재공시)(?:\s*내용)?입니다\.?\s*$')


def parse_inquiry(kv: dict) -> list:
    """조회공시요구(풍문·보도/현저한 시황변동)와 그 답변(확정·미확정·부인).

    답변: 1.제목 / 2.답변내용(또는 2.내용) / 조회공시요구일·답변일 / 재공시 기한(또는 예정일).
    요구: 조회공시요구내용 / 답변(공시)시한. 답변 본문은 절단 없이 항목별로 —
    파이온엑스 파산신청설 답변이 범용 폴백 100자에서 '…관련문서 등을 송달받지 못하여'
    핵심 직전에 잘리던 문제.
    """
    lines = []
    if '답변' in kv.get('_report_nm', ''):
        if v := _get(kv, '1. 제목', '제목'):
            lines.append(f'📌 {_trunc_clean(v, 150)}')
        lines += [b for b in _answer_bullets(_get(kv, '2. 답변내용', '답변내용', '2. 내용', '내용') or '')
                  if not _ANS_BOILER.match(b)]     # '본 공시는 …에 대한 답변입니다' 상투문
        req = _get(kv, '조회공시요구일')
        ans = _get(kv, '조회공시답변일')
        if req or ans:
            lines.append('📅 ' + ' · '.join(x for x in (f'요구일: {req}' if req else '',
                                                        f'답변일: {ans}' if ans else '') if x))
        due = _get(kv, '재공시예정일') or next(
            (v for k, v in kv.items() if k.strip() == '기한' and re.search(r'\d{4}', v or '')), None)
        if due:
            lines.append(f'⏰ 재공시 기한: {due}')
    else:
        if v := _get(kv, '조회공시요구내용', '조회공시 요구내용', '요구내용'):
            lines.append(f'❓ 요구내용: {_trunc_clean(v, 200)}')
        if dl := _get(kv, '답변시한', '공시시한'):
            tm = (kv.get(dl) or '').strip()        # '4. 답변시한: 2026-09-29' + '2026-09-29: 18:00까지'
            lines.append(f'⏰ 답변시한: {dl}' + (f' {tm}' if re.search(r'\d{1,2}:\d{2}', tm) else ''))
    return lines


def parse_rumor_reply(kv: dict) -> list:
    """풍문또는보도에대한해명 — 보도 내용·매체·일자, 해명(또는 미확정 재공시 안내), 재공시예정일."""
    lines = []
    if v := _get(kv, '풍문 또는 보도의 내용', '보도의 내용'):
        lines.append(f'📰 보도: {_trunc_clean(v, 200)}')
    src = _get(kv, '보도의 매체')
    dt = _get(kv, '보도의 발생일자', '발생일자')
    if src or dt:
        lines.append('🗞 ' + ' · '.join(x for x in (src, dt) if x))
    if exp := _get(kv, '해명내용', '해명 내용'):
        lines.append('📋 해명:')
        lines += _answer_bullets(exp)
    else:   # 미확정 재공시: 키 '2026-09-03' → 값 '일자 … 해명(미확정)의 재공시 사항임'
        for k, v in kv.items():
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', k.strip()) and '재공시' in (v or ''):
                note = re.sub(r'\s+', ' ', v).strip()
                lines.append(f'📋 {k.strip()} {note}')
                break
    if v := _get(kv, '재공시예정일'):
        lines.append(f'⏰ 재공시예정일: {v}')
    return lines if len(lines) > 1 else []


_FAIR_SEC = re.compile(
    r'(?:^|\s)\d\.\s*(공시제목|공정공시\s*대상정보|공정공시\s*정보|주요내용|연락처[^:：]*?|'
    r'기타\s*투자판단에\s*참고할\s*사항|참고사항)\s*[:：]?\s*')


def parse_fair_disclosure(kv: dict) -> list:
    """수시공시의무관련사항(공정공시) — 표 없는 산문형(1.공시제목/2.공정공시 정보/4.기타/5.참고사항
    또는 1.공정공시 대상정보/2.주요내용)과 표형(공시제목·관련 수시공시내용) 모두.
    에코프로비엠 '대표이사 변경 예정'(2026-10-01)이 KV 0개라 본문 없이 발송되던 문제."""
    lines = []
    if title := _get(kv, '공시제목'):                       # 표형(리츠 등)
        lines.append(f'📌 {_trunc_clean(title, 150)}')
        lines += _answer_bullets(_get(kv, '관련 수시공시내용', '수시공시내용', '주요내용') or '')
        ev = _get(kv, '행사명')
        when = _get(kv, '정보제공(예정)일시', '정보제공일시')
        if ev or when:
            lines.append('🗓 정보제공: ' + ' · '.join(x for x in (ev, when) if x))
        if v := _get(kv, '관련공시'):
            lines.append(f'🔗 관련: {_rel_text(v)}')
        return lines if len(lines) > 1 else []

    raw = kv.get('_html', '')
    txt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ',
                 re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', raw, flags=re.S | re.I))).strip()
    parts = _FAIR_SEC.split(txt)
    sec = {}
    for i in range(1, len(parts) - 1, 2):
        key = re.sub(r'\s+', '', parts[i])
        sec.setdefault(key, parts[i + 1].strip())
    title = sec.get('공시제목') or sec.get('공정공시대상정보')
    if title:
        lines.append(f'📌 {_trunc_clean(title.lstrip("- ").strip(), 150)}')
    lines += _answer_bullets(sec.get('공정공시정보') or sec.get('주요내용') or '')
    if v := sec.get('기타투자판단에참고할사항'):
        notes = _etc_segments(v)
        if notes:
            lines.append(f'📎 참고: {_trunc_clean(" ".join(notes), 300)}')
    if v := sec.get('참고사항'):
        notes = _etc_segments(v)
        if notes:
            lines.append(f'📎 참고사항: {_trunc_clean(" · ".join(notes), 350)}')
    return lines if len(lines) > 1 else []


def parse_business_suspension(kv: dict) -> list:
    """영업정지(주요사항보고서/거래소) — 분야·정지금액(매출 대비)·내용·사유·영향·향후대책·일자.
    국문 키 + 국/영문 이중언어 서식의 영문 키 모두. 정정 서식은 정정표 행('5.향후대책: -진행사항
    추가')이 본문 키보다 앞서 부분일치되므로 '5. 향후대책'처럼 번호+공백 키를 먼저 찾는다."""
    lines = []
    if v := _get(kv, '영업정지 분야', 'Suspended business operations'):
        lines.append(f'⛔ 분야: {_trunc_clean(v, 120)}')
    amt = None
    for key in ('영업정지금액', 'Amount of business suspension', '영업정지내역', '영업정지 내역'):
        cand = _get(kv, key)
        if cand and re.fullmatch(r'[\d,]+', cand.replace(' ', '')):
            amt = cand
            break
    ratio = _get(kv, '매출액 대비', '매출액대비', 'Ratio to sales')
    if amt:
        lines.append(f'💰 정지금액: {_fmt_amount(amt)}원' + (f' (매출 대비 {ratio}%)' if ratio else ''))
    for label, keys in (('📋 내용', ('3. 영업정지 내용', '영업정지 내용', 'Details of business suspension')),
                        ('🚨 사유', ('4. 영업정지사유', '영업정지사유', '영업정지 사유',
                                    'Reasons for business suspension')),
                        ('📉 영향', ('6. 영업정지영향', '6. 영업정지 영향', 'Impact of business suspension')),
                        ('🔧 향후대책', ('5. 향후대책', 'Actions to be taken'))):
        if v := _get(kv, *keys):
            v = re.sub(r'^-\s*', '', v).strip()
            if v:
                lines.append(f'{label}: {_trunc_clean(v, 300)}')
    if v := _get(kv, '7. 영업정지일자', '영업정지일자', 'Effective date of business suspension'):
        lines.append(f'📅 정지일: {v}')
    if v := _get(kv, '8. 이사회결의일', '이사회결의일', 'Board resolution date'):
        lines.append(f'📅 결정일: {v}')
    if v := _get(kv, '기타 투자판단'):
        notes = _etc_segments(v)
        if notes:
            lines.append(f'📎 참고: {_trunc_clean(" ".join(notes), 300)}')
    return lines if len(lines) > 1 else []


def _split_num(v):
    v = (v or '').strip()
    return int(v.replace(',', '')) if re.fullmatch(r'[\d,]+', v) and v.replace(',', '') else None


def _split_money(label: str, v) -> str:
    n = _split_num(v)
    return f'{label} {_fmt_amount(str(n))}원' if n else ''


def _split_ymd(s: str):
    m = re.search(r'(\d{4})\s*[년.\-]\s*(\d{1,2})\s*[월.\-]\s*(\d{1,2})', s or '')
    return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}' if m else None


def parse_split(kv: dict) -> list:
    """회사분할결정 — 분할방식(인적/물적·비율), 존속/신설 회사별 재무·매출·상장 여부, 일정.

    국/영문 이중언어 서식이라 범용 폴백이 영문 라벨과 값이 한 칸씩 밀린 채 덤프 —
    현대모비스(2026-09-30) '자산총계/부채총계 541,529,971,210'처럼 존속·신설 수치가 뒤섞임.
    원문 텍스트의 국문 라벨('분할 후 존속회사 … 자산총계 … 분할 후 상장유지 여부',
    '분할설립회사 … 재상장신청 여부', '분할기일')로 직접 추출."""
    nm = kv.get('_report_nm', '')
    if '분할합병' in nm:
        return []
    raw = kv.get('_html', '')
    t = re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', t)).strip()
    if '분할' not in t:
        return []
    lines = []

    # 분할 방식 + 비율
    # '분할방법' 첫 등장은 정정표 행일 수 있음 — 인적/물적 문구가 있는 첫 구간 사용
    meth = next((t[m_.end():m_.end() + 900] for m_ in re.finditer(r'분할방법', t)
                 if re.search(r'(?:인적|물적)분할', t[m_.end():m_.end() + 900])), '')
    kind = ('인적분할' if '인적분할' in meth else '물적분할' if '물적분할' in meth else '')
    if kind:
        simple = '단순·' if re.search(r'단순\s*[·ㆍ]?\s*(?:인적|물적)', meth) else ''
        if kind == '물적분할':
            lines.append(f'✂️ {simple}물적분할 — 신설회사 지분 100%를 존속회사가 보유')
        else:
            rs = re.search(r'분할존속회사\s*[:：]\s*([\d.]+)', t)
            rn = re.search(r'분할신설회사\s*[:：]\s*([\d.]+)', t)
            ratio = f' — 비율 존속 {rs.group(1)} : 신설 {rn.group(1)}' if rs and rn else ''
            lines.append(f'✂️ {simple}인적분할 (기존 주주가 신설회사 주식 배정){ratio}')

    # 존속회사
    ms = re.search(r'분할\s*후\s*존속회사\s*회사명\s*(.+?)\s*분할\s*후\s*재무내용\s*\(원\)\s*'
                   r'자산총계\s*(\S+)\s*부채총계\s*(\S+)\s*자본총계\s*(\S+)\s*자본금\s*(\S+)'
                   r'.*?매출액\s*\(원\)\s*(\S+)\s*주요사업\s*(.+?)\s*분할\s*후\s*상장유지\s*여부\s*(\S+)', t)
    if ms:
        name, a, l_, e, _cap, sales, _biz, keep = ms.groups()
        st = ' · 상장유지 예' if keep.startswith(('예', 'Y')) else (' · 상장유지 아니오' if keep.startswith(('아니', 'N')) else '')
        lines.append(f'🏢 존속: {_trunc_clean(name, 60)}{st}')
        fin = [x for x in (_split_money('자산', a), _split_money('부채', l_), _split_money('자본', e),
                           _split_money('매출', sales)) if x]
        if fin:
            lines.append('     ' + ' · '.join(fin))

    # 신설회사(복수 가능)
    for mn in re.finditer(r'분할\s*설립회사\s*회사명\s*(.+?)\s*(?:설립시\s*재무내용\s*\(원\)\s*'
                          r'자산총계\s*(\S+)\s*부채총계\s*(\S+)\s*자본총계\s*(\S+)\s*)?자본금\s*\(?원?\)?\s*(\S+)'
                          r'(?:.*?매출액\s*\(원\)\s*(\S+))?\s*주요사업\s*(.+?)\s*'
                          r'(?:재상장\s*신청\s*여부\s*(\S+)|\d{1,2}\.\s*감자에)', t):
        name, a, l_, e, cap, sales, biz, relist = mn.groups()
        st = ''
        if relist:
            st = ' · 재상장 신청' if relist.startswith(('예', 'Y')) else ' · 재상장 신청 안 함'
        lines.append(f'🆕 신설: {_trunc_clean(name, 60)}{st}')
        fin = [x for x in (_split_money('자산', a), _split_money('부채', l_), _split_money('자본', e),
                           _split_money('매출', sales)) if x] or [x for x in (_split_money('자본금', cap),) if x]
        if fin:
            lines.append('     ' + ' · '.join(fin))
        lines.append(f'     사업: {_trunc_clean(biz, 120)}')
        if len(lines) > 8:
            break

    # 일정
    sched = []
    _DT = r'(\d{4}\s*[년.\-]\s*\d{1,2}\s*[월.\-]\s*\d{1,2})'
    # 라벨 첫 등장은 '분할기일 현재 …' 같은 문장일 수 있어 날짜가 바로 붙은 첫 위치를 사용
    if m_ := re.search(r'주주총회\s*예정일(?:자)?\s*' + _DT, t):
        sched.append(f'주총 {_split_ymd(m_.group(1))}')
    if m_ := re.search(r'분할기일\s*(?:은|:)?\s*(?:\(예정\)\s*)?' + _DT, t):
        sched.append(f'분할기일 {_split_ymd(m_.group(1))}')
    mh = re.search(r'매매거래정지\s*예정기간\s*시작일\s*(\S+\s*\S*\s*\S*)\s*종료일\s*(\S+\s*\S*\s*\S*)', t)
    if mh and _split_ymd(mh.group(1)) and _split_ymd(mh.group(2)):
        sched.append(f'거래정지 {_split_ymd(mh.group(1))}~{_split_ymd(mh.group(2))}')
    if sched:
        lines.append('📅 ' + ' · '.join(sched))

    # 목적 — 국문 '분할목적' 섹션 첫 문장(영문 키 값도 국문)
    pur = _get(kv, 'Purpose of split-off', '분할목적')
    if not pur:
        mp = re.search(r'분할\s*목적\s*(.+?)\s*3\.\s*분할의\s*중요', t)
        pur = mp.group(1) if mp else ''
    pur = re.sub(r'^\(?1\)\s*', '', re.sub(r'\s+', ' ', pur or '')).strip()
    if pur:
        first = re.split(r'(?<=다\.)\s', pur)[0]
        lines.append(f'🎯 목적: {_trunc_clean(first, 200)}')
    return lines if len(lines) > 1 else []
