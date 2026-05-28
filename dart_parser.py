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
    """키 후보들 중 첫 번째로 부분일치하는 값 반환.
    기재정정 공시에서는 '정정후' 키를 '정정전' 키보다 우선 반환."""
    for key in keys:
        # 1순위: '정정후' 포함 키 (기재정정 공시의 수정된 값)
        for k, v in kv.items():
            if key in k and '정정후' in k:
                clean = re.sub(r'\s+', ' ', v).strip()
                if clean and clean not in ('-', '—', '없음', 'N/A'):
                    return clean
        # 2순위: 일반 키 ('정정전' 제외 — 기재정정이 아닌 경우엔 동일하게 동작)
        for k, v in kv.items():
            if key in k and '정정전' not in k:
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


def parse_dividend(kv: dict) -> list:
    """현금ㆍ현물배당결정"""
    lines = []
    if v := _get(kv, '주당배당금', '1주당 배당금', '배당금액'):
        lines.append(f'💰 주당배당금: {v}')
    if v := _get(kv, '시가배당률', '배당수익률'):
        lines.append(f'📊 시가배당률: {v}')
    if v := _get(kv, '배당기준일', '주주확정일'):
        lines.append(f'📅 배당기준일: {v}')
    if v := _get(kv, '배당지급예정일', '지급예정일', '배당금지급일'):
        lines.append(f'📅 지급예정일: {v}')
    return lines


def parse_acquisition(kv: dict) -> list:
    """타법인주식및출자증권취득결정"""
    lines = []
    if v := _get(kv, '취득대상회사', '피투자회사', '취득법인'):
        lines.append(f'🏢 취득대상: {v}')
    if v := _get(kv, '취득금액', '취득예정금액'):
        lines.append(f'💰 취득금액: {v}')
    if v := _get(kv, '취득후지분율', '취득 후 지분율', '지분율'):
        lines.append(f'📊 취득 후 지분: {v}')
    if v := _get(kv, '취득목적', '취득사유', '목적'):
        lines.append(f'📋 목적: {_trunc(v)}')
    return lines


def parse_facility_investment(kv: dict) -> list:
    """신규시설투자등"""
    lines = []
    if v := _get(kv, '투자내용', '시설내용', '투자목적물'):
        lines.append(f'🏭 투자내용: {_trunc(v)}')
    if v := _get(kv, '투자금액', '총투자금액'):
        lines.append(f'💰 투자금액: {v}')
    if v := _get(kv, '투자기간', '사업기간', '준공예정'):
        lines.append(f'📅 투자기간: {v}')
    if v := _get(kv, '투자목적', '투자사유', '목적'):
        lines.append(f'📋 목적: {_trunc(v)}')
    return lines


def parse_large_holding(kv: dict) -> list:
    """주식등의대량보유상황보고서"""
    lines = []
    if v := _get(kv, '보고자', '대량보유자'):
        lines.append(f'👤 보고자: {v}')
    if v := _get(kv, '보유주식수', '보유수량'):
        lines.append(f'🔢 보유주식수: {v}')
    if v := _get(kv, '보유비율', '지분율'):
        lines.append(f'📊 보유비율: {v}')
    if v := _get(kv, '변동사유', '보유목적'):
        lines.append(f'📋 사유: {_trunc(v)}')
    return lines


def parse_treasury_dispose(kv: dict) -> list:
    """자기주식처분결정"""
    lines = []
    if v := _get(kv, '처분주식수', '처분예정주식수'):
        lines.append(f'🔢 처분주식수: {v}')
    if v := _get(kv, '처분금액', '처분예정금액', '1주당처분가액'):
        lines.append(f'💰 처분가액: {v}')
    if v := _get(kv, '처분기간', '처분예정기간'):
        lines.append(f'📅 처분기간: {v}')
    if v := _get(kv, '처분목적', '처분사유'):
        lines.append(f'📋 목적: {_trunc(v)}')
    return lines


def parse_investment_decision(kv: dict) -> list:
    """투자판단관련주요경영사항 (일반)"""
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


def parse_clinical_trial(kv: dict) -> list:
    """투자판단관련주요경영사항(임상시험결과)"""
    lines = []
    if v := _get(kv, '시험약명', '후보물질', '시험물질', '의약품명', '물질명'):
        lines.append(f'🔬 후보물질: {v}')
    if v := _get(kv, '임상단계', '시험단계', '개발단계'):
        lines.append(f'📊 임상단계: {v}')
    if v := _get(kv, '대상질환', '적응증', '목표질환', '질환'):
        lines.append(f'💊 대상질환: {v}')
    if v := _get(kv, '임상결과', '시험결과', '결과'):
        lines.append(f'📋 결과: {_trunc(v)}')
    # 결과 필드 없을 때 fallback
    if not lines or len(lines) < 2:
        if v := _get(kv, '주요내용', '주요 내용', '내용'):
            lines.append(f'📋 내용: {_trunc(v)}')
    return lines


def parse_clinical_trial_plan(kv: dict) -> list:
    """투자판단관련주요경영사항(임상시험계획승인신청등결정)
    IND 신청 / 임상시험 계획 승인 관련 공시.
    """
    lines = []
    if v := _get(kv, '시험약명', '후보물질', '시험물질', '의약품명', '물질명'):
        lines.append(f'🔬 후보물질: {v}')
    if v := _get(kv, '임상단계', '시험단계', '개발단계'):
        lines.append(f'📊 임상단계: {v}')
    if v := _get(kv, '대상질환', '적응증', '목표질환', '질환'):
        lines.append(f'💊 대상질환: {v}')
    if v := _get(kv, '신청기관', '승인기관', '규제기관', '제출기관'):
        lines.append(f'🏛️ 신청기관: {v}')
    if v := _get(kv, '신청일', '결정일', '승인일', '제출일'):
        lines.append(f'📅 신청일: {v}')
    # 주요내용 fallback
    if not lines or len(lines) < 2:
        if v := _get(kv, '주요내용', '주요 내용', '내용'):
            lines.append(f'📋 내용: {_trunc(v)}')
    return lines


# ══════════════════════════════════════════════
#  공시 타입 → 파서 매핑
#  ※ 구체적인 타입을 먼저, 일반 타입을 나중에 등록
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
    (['현금', '현물배당'],                          parse_dividend),
    (['타법인주식', '출자증권취득'],                 parse_acquisition),
    (['신규시설투자'],                              parse_facility_investment),
    (['주식등의대량보유'],                          parse_large_holding),
    (['자기주식처분결정'],                          parse_treasury_dispose),
    (['임상시험계획승인신청'],                       parse_clinical_trial_plan),
    (['임상시험결과'],                              parse_clinical_trial),
    (['투자판단관련주요경영사항'],                   parse_investment_decision),  # 일반 (fallback)
]


# ══════════════════════════════════════════════
#  공개 인터페이스
# ══════════════════════════════════════════════

def get_disclosure_detail(rcept_no: str, report_nm: str) -> str:
    """
    공시 원문을 파싱하여 핵심 필드 텍스트 반환.
    파싱 실패 또는 미지원 타입이면 빈 문자열 반환 (graceful fallback).
    ※ [기재정정] 공시: _get()이 '정정후' 키를 우선 반환하므로 자동 처리됨.
    """
    # [기재정정] 등 접두어 제거 후 파서 선택 (예: "[기재정정]단일판매·공급계약체결" → 계약 파서)
    clean_nm = re.sub(r'^\[[^\]]+\]', '', report_nm).strip()
    is_amendment = report_nm.startswith('[기재정정]')

    # 파서 선택
    parser = None
    for keywords, fn in _PARSER_MAP:
        if any(k in clean_nm for k in keywords):
            parser = fn
            break
    if parser is None:
        return ''

    # HTML 가져오기
    html = _fetch_html(rcept_no)
    if not html:
        log.debug(f'[DART 파서] HTML 없음 ({rcept_no})')
        return ''

    # 파싱
    try:
        kv = _build_kv(html)
        if is_amendment:
            log.debug(f'[DART 파서] 기재정정 kv 키 목록: {list(kv.keys())[:10]}')
        lines = parser(kv)
        if not lines:
            log.debug(f'[DART 파서] 파싱 결과 없음 ({report_nm}) — kv 키: {list(kv.keys())[:5]}')
        return '\n'.join(lines)
    except Exception as e:
        log.warning(f'[DART 파서] 파싱 실패 ({report_nm}): {e}')
        return ''
