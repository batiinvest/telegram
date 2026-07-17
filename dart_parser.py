"""
dart_parser.py — DART 공시 원문 구조화 파서 (공개 인터페이스·파사드)

공시 HTML에서 핵심 필드를 추출하여 텔레그램 메시지용 텍스트로 반환.
파싱 실패 / HTML 없으면 빈 문자열 반환 (graceful fallback).

[구조 — 2026-07-17 3분할]
  dart_doc.py      원문 취득(document.xml→zip.do→뷰어) · _build_kv · 공용 유틸
  dart_parsers.py  카테고리별 파서 30여 종 + _PARSER_MAP 등록제
  dart_parser.py   get_disclosure_detail · get_audit_opinion · PARSER_STATS
                   (+기존 임포트 하위호환 재수출)
"""
import re
import logging
from collections import Counter

from dart_doc import (          # 사용 + 하위호환 재수출
    _fetch_html, _build_kv, parse_all_fields,
    _fetch_dart_list_item, _fetch_dart_reporter, _fetch_dart_majorstock,
    _get, _trunc, _trunc_clean,
)
from dart_parsers import *      # parse_* 전 파서 재수출 (하위호환)
from dart_parsers import _PARSER_MAP, _SKIP_DETAIL_TYPES, parse_amendment

log = logging.getLogger(__name__)


# 파서 적중률 통계 — get_disclosure_detail이 증가, 공시봇 일일요약이 읽고 clear.
# 키: 카테고리 파서명 / 'amendment' / 'fallback' / 'empty' / 'no_html' / 'skip_type' / 'error'
PARSER_STATS: Counter = Counter()


_AUDIT_OPINION_PAT = re.compile(
    r'(?:감사의견|검토의견)[^가-힣]{0,20}(의견\s*거절|부적정|비적정|한정|적정)')


def get_audit_opinion(rcept_no: str) -> str | None:
    """감사·검토보고서 제출 공시에서 감사의견(+부가 위험 항목) 추출.

    제목엔 의견이 드러나지 않아('감사보고서제출') 비적정 여부는 원문에서만
    판별 가능 — 공시봇이 skip 등급을 urgent로 승격할 때 사용.
    의견이 적정이라도 실질심사·관리종목 사유가 되는 부가 항목(계속기업
    존속불확실성 해당, 횡령ㆍ배임 기재, 내부회계 비적정)이 있으면
    '적정(사유)' 형태로 반환 → 호출측 '적정' 비교에서 승격됨.
    반환: '적정' | '적정(…)' | '한정' | '부적정' | '비적정' | '의견거절' | None.
    """
    html = _fetch_html(rcept_no)
    if not html:
        return None
    txt = re.sub(r'<[^>]+>', ' ', html)
    txt = re.sub(r'\s+', ' ', txt)
    m = _AUDIT_OPINION_PAT.search(txt)
    opinion = m.group(1).replace(' ', '') if m else None
    if opinion == '적정':
        flags = []
        # '해당여부 해당'만 매칭 ('미해당'은 '미' 뒤 '해당'이라 매칭 안 됨)
        if re.search(r'존속\s*불확실성\s*사유\s*해당\s*여부\s*해당', txt):
            flags.append('계속기업 불확실성')
        if re.search(r'횡령[ㆍ·]?\s*배임사항\s*기재\s*여부\s*예', txt):
            flags.append('횡령·배임 기재')
        if re.search(r'내부회계관리제도\s*감사\(검토\)의견\s*비적정\s*등\s*여부\s*해당', txt):
            flags.append('내부회계 비적정')
        if flags:
            return f"적정({'·'.join(flags)})"
    return opinion


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
    # 카테고리 판정용 제목 ([기재정정] 등 접두어 제거)
    clean_nm = re.sub(r'^\[[^\]]+\]', '', report_nm).strip()

    # 상세 불필요 공시 — 원문 fetch 전에 판정 (정기보고서류는 문서가 커서 fetch 자체 생략)
    if any(skip in clean_nm for skip in _SKIP_DETAIL_TYPES):
        PARSER_STATS['skip_type'] += 1
        return ''

    html = _fetch_html(rcept_no)
    if not html:
        PARSER_STATS['no_html'] += 1
        log.debug(f'[DART 파서] HTML 없음 ({rcept_no})')
        return ''

    try:
        kv = _build_kv(html)
        if not kv:
            # 표 없는 산문 문서(KRX 기타시장안내 등) — 조기 반환하지 않고
            # 카테고리 파서의 _html 폴백에 기회를 준다
            log.debug(f'[DART 파서] KV 없음 — 산문 폴백 시도 ({report_nm})')
        kv['_html'] = html        # 일부 파서에서 원문 직접 파싱 용도
        kv['_rcept_no'] = rcept_no  # API fallback용 접수번호

        if report_nm.startswith('[기재정정]'):
            log.debug(f'[DART 파서] 기재정정 kv 키: {list(kv.keys())[:10]}')

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
                has_changes = any(l.startswith('🔧') for l in lines)
                lines.insert(0, '🔄 정정 내용')
                # 카테고리 파서 또는 범용 파서로 원문 내용 추가
                sub = (parser(kv) if parser else None) or (parse_all_fields(kv) if not has_changes else None)
                if sub:
                    lines.append('════════════')
                    lines.extend(sub)
                PARSER_STATS['amendment'] += 1
                return '\n'.join(lines)

        if parser:
            lines = parser(kv)
            if lines:
                PARSER_STATS[parser.__name__] += 1
                log.debug(f'[DART 파서] 카테고리 파서 사용 ({report_nm})')
                return '\n'.join(lines)

        # 범용 파서 fallback
        lines = parse_all_fields(kv)
        PARSER_STATS['fallback' if lines else 'empty'] += 1
        if not lines:
            log.debug(f'[DART 파서] 파싱 결과 없음 ({report_nm})')
        return '\n'.join(lines)

    except Exception as e:
        PARSER_STATS['error'] += 1
        log.warning(f'[DART 파서] 파싱 실패 ({report_nm}): {e}')
        return ''
