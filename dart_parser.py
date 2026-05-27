"""
dart_parser.py — DART 공시 원문 구조화 파서 (방식 B)

공시 HTML에서 핵심 필드를 추출하여 텔레그램 메시지용 텍스트로 반환.
파싱 실패 / 미지원 타입이면 빈 문자열 반환 (graceful fallback).
"""

import re
import logging
from bs4 import BeautifulSoup
from managers import global_session as _session

log = logging.getLogger(__name__)

_MOBILE_UA = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1'
)


# ══════════════════════════════════════════════
#  HTML 가져오기
# ══════════════════════════════════════════════

def _fetch_html(rcept_no: str) -> str | None:
    """모바일 DART 페이지 HTML 가져오기."""
    url = f'http://m.dart.fss.or.kr/html_mdart/MD1007.html?rcpNo={rcept_no}'
    try:
        res = _session.get(url, headers={'User-Agent': _MOBILE_UA}, timeout=5)
        res.encoding = 'utf-8'
        if res.status_code == 200:
            return res.text
    except Exception as e:
        log.warning(f'[DART 파서] HTML 요청 실패 ({rcept_no}): {e}')
    return None


# ══════════════════════════════════════════════
#  KV 추출
# ══════════════════════════════════════════════

def _build_kv(html: str) -> dict:
    """테이블 모든 행에서 key→value 매핑 추출."""
    soup = BeautifulSoup(html, 'html.parser')
    kv: dict = {}
    for row in soup.find_all('tr'):
        cells = [re.sub(r'\s+', ' ', c.get_text(' ', strip=True))
                 for c in row.find_all(['td', 'th'])]
        # 2셀: (key, val)
        if len(cells) == 2 and cells[0] and cells[1]:
            kv[cells[0]] = cells[1]
        # 4셀: (key1, val1, key2, val2)
        elif len(cells) == 4:
            if cells[0] and cells[1]:
                kv[cells[0]] = cells[1]
            if cells[2] and cells[3]:
                kv[cells[2]] = cells[3]
    return kv


def _get(kv: dict, *keys: str) -> str | None:
    """키 후보들 중 첫 번째로 부분일치하는 값 반환."""
    for key in keys:
        for k, v in kv.items():
            if key in k:
                clean = re.sub(r'\s+', ' ', v).strip()
                if clean and clean not in ('-', '—', '없음', 'N/A'):
                    return clean
    return None


def _trunc(text: str, limit: int = 55) -> str:
    return text[:limit] + '…' if len(text) > limit else text


# ══════════════════════════════════════════════
#  공시 타입별 파서
# ══════════════════════════════════════════════

def parse_contract(kv: dict) -> list:
    """단일판매ㆍ공급계약체결 / 수주"""
    lines = []
    if v := _get(kv, '계약상대방', '거래상대방', '발주처', '매수인'):
        lines.append(f'📋 계약상대: {v}')
    if v := _get(kv, '계약금액', '공급금액', '수주금액', '거래금액'):
        lines.append(f'💰 금액: {v}')
    if v := _get(kv, '최근매출액대비', '매출액대비'):
        lines.append(f'📊 매출비중: {v}')
    if v := _get(kv, '계약기간', '공급기간', '납기일'):
        lines.append(f'📅 기간: {v}')
    return lines


def parse_rights_offering(kv: dict) -> list:
    """유상증자결정"""
    lines = []
    if v := _get(kv, '증자방식', '발행방법'):
        lines.append(f'📋 방식: {v}')
    if v := _get(kv, '신주식수', '발행주식수', '신주의수'):
        lines.append(f'🔢 신주: {v}')
    if v := _get(kv, '1주당 발행가액', '발행가액'):
        lines.append(f'💵 발행가: {v}')
    if v := _get(kv, '자금조달목적', '조달금액', '모집총액'):
        lines.append(f'💰 조달: {v}')
    if v := _get(kv, '납입일'):
        lines.append(f'📅 납입일: {v}')
    return lines


def parse_bonus_issue(kv: dict) -> list:
    """무상증자결정"""
    lines = []
    if v := _get(kv, '신주배정주식수', '신주식수', '신주의수'):
        lines.append(f'🔢 신주: {v}')
    if v := _get(kv, '1주당 신주배정', '1주당 배정주식수', '배정비율'):
        lines.append(f'📋 배정비율: {v}')
    if v := _get(kv, '신주배정기준일', '기준일'):
        lines.append(f'📅 기준일: {v}')
    if v := _get(kv, '신주의상장예정일', '상장예정일'):
        lines.append(f'📅 상장예정: {v}')
    return lines


def parse_cb_bw(kv: dict) -> list:
    """전환사채(CB) / 신주인수권부사채(BW)"""
    lines = []
    if v := _get(kv, '발행금액', '사채금액', '권면총액'):
        lines.append(f'💰 발행금액: {v}')
    if v := _get(kv, '전환가액', '행사가액'):
        lines.append(f'💵 전환가액: {v}')
    if v := _get(kv, '표면이자율', '이자율'):
        lines.append(f'📊 이자율: {v}')
    if v := _get(kv, '만기일'):
        lines.append(f'📅 만기: {v}')
    if v := _get(kv, '전환청구기간', '행사청구기간', '행사기간'):
        lines.append(f'📅 청구기간: {v}')
    return lines


def parse_shareholder_change(kv: dict) -> list:
    """최대주주변경"""
    lines = []
    if v := _get(kv, '변경전 최대주주', '변경전최대주주', '기존 최대주주'):
        lines.append(f'👤 변경전: {v}')
    if v := _get(kv, '변경후 최대주주', '변경후최대주주', '신규 최대주주'):
        lines.append(f'👤 변경후: {v}')
    if v := _get(kv, '변경사유', '변경이유'):
        lines.append(f'📋 사유: {_trunc(v)}')
    if v := _get(kv, '변경일'):
        lines.append(f'📅 변경일: {v}')
    return lines


def parse_earnings(kv: dict) -> list:
    """잠정실적"""
    lines = []
    if v := _get(kv, '매출액'):
        lines.append(f'💰 매출액: {v}')
    if v := _get(kv, '영업이익'):
        lines.append(f'📊 영업이익: {v}')
    if v := _get(kv, '당기순이익', '순이익'):
        lines.append(f'📊 순이익: {v}')
    return lines


def parse_merger(kv: dict) -> list:
    """합병결정"""
    lines = []
    if v := _get(kv, '합병방법', '합병방식'):
        lines.append(f'📋 방식: {v}')
    if v := _get(kv, '피합병회사', '합병대상회사', '소멸회사'):
        lines.append(f'🏢 피합병: {v}')
    if v := _get(kv, '합병비율'):
        lines.append(f'📋 비율: {v}')
    if v := _get(kv, '합병기일', '합병예정일'):
        lines.append(f'📅 합병기일: {v}')
    return lines


def parse_mgmt_issue(kv: dict) -> list:
    """관리종목지정"""
    lines = []
    if v := _get(kv, '지정사유', '지정내용', '조치내용'):
        lines.append(f'⚠️ 사유: {_trunc(v)}')
    if v := _get(kv, '지정일', '조치일'):
        lines.append(f'📅 지정일: {v}')
    return lines


def parse_halt(kv: dict) -> list:
    """매매거래정지"""
    lines = []
    if v := _get(kv, '거래정지사유', '정지사유', '조치내용', '내용'):
        lines.append(f'⚠️ 사유: {_trunc(v)}')
    if v := _get(kv, '정지일', '시행일', '조치일'):
        lines.append(f'📅 정지일: {v}')
    return lines


def parse_investment_decision(kv: dict) -> list:
    """투자판단관련주요경영사항"""
    lines = []
    if v := _get(kv, '주요내용', '결정내용', '주요 내용', '내용'):
        lines.append(f'📋 내용: {_trunc(v)}')
    if v := _get(kv, '거래상대방', '계약상대방', '상대방', '피투자회사'):
        lines.append(f'🏢 상대방: {v}')
    if v := _get(kv, '금액', '거래금액', '투자금액', '취득금액'):
        lines.append(f'💰 금액: {v}')
    if v := _get(kv, '결정사유', '사유', '목적'):
        lines.append(f'📋 사유: {_trunc(v)}')
    return lines


# ══════════════════════════════════════════════
#  공시 타입 → 파서 매핑
# ══════════════════════════════════════════════

_PARSER_MAP = [
    (['계약', '수주'],                              parse_contract),
    (['유상증자'],                                  parse_rights_offering),
    (['무상증자'],                                  parse_bonus_issue),
    (['전환사채', '신주인수권부사채'],               parse_cb_bw),
    (['최대주주변경'],                              parse_shareholder_change),
    (['잠정실적', '매출액또는손익구조'],             parse_earnings),
    (['합병'],                                      parse_merger),
    (['관리종목'],                                  parse_mgmt_issue),
    (['거래정지', '매매거래정지'],                   parse_halt),
    (['투자판단관련주요경영사항'],                   parse_investment_decision),
]


# ══════════════════════════════════════════════
#  공개 인터페이스
# ══════════════════════════════════════════════

def get_disclosure_detail(rcept_no: str, report_nm: str) -> str:
    """
    공시 원문을 파싱하여 핵심 필드 텍스트 반환.
    파싱 실패 또는 미지원 타입이면 빈 문자열 반환 (graceful fallback).
    """
    # 파서 선택
    parser = None
    for keywords, fn in _PARSER_MAP:
        if any(k in report_nm for k in keywords):
            parser = fn
            break
    if parser is None:
        return ''

    # HTML 가져오기
    html = _fetch_html(rcept_no)
    if not html:
        return ''

    # 파싱
    try:
        kv = _build_kv(html)
        lines = parser(kv)
        return '\n'.join(lines)
    except Exception as e:
        log.warning(f'[DART 파서] 파싱 실패 ({report_nm}): {e}')
        return ''
