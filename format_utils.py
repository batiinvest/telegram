"""
format_utils.py — 공통 포맷 유틸
──────────────────────────────────
숫자·날짜·등락률 포맷 함수를 한 곳에서 관리합니다.
프론트엔드 config.js의 chgStr / fmtCap / fmtNet과 동일한 로직 유지.

사용법:
    from format_utils import fmt_change_pct, fmt_cap, fmt_net, fmt_money
"""

from typing import Optional


# ── 등락률 ────────────────────────────────────────────────────────────────────

def fmt_change_pct(v: Optional[float], dec: int = 2) -> str:
    """등락률 문자열 (예: +2.34% / -1.50% / —)"""
    if v is None:
        return '—'
    sign = '+' if v > 0 else ''   # 0은 부호 없음(프론트 chgStr와 정합)
    return f'{sign}{v:.{dec}f}%'


def chg_icon(v: Optional[float]) -> str:
    """등락 방향 아이콘 (🔺/🔽/━)"""
    if v is None or v == 0:
        return '━'
    return '🔺' if v > 0 else '🔽'


# ── 금액/시총 ─────────────────────────────────────────────────────────────────

def fmt_cap(won: Optional[int]) -> str:
    """
    원 단위 → 조/억 표시 (JS fmtCap 동일 로직)
    예: 1_500_000_000_000 → '1조 5,000억'
    """
    if won is None:
        return '—'
    abs_won = abs(won)
    sign    = '-' if won < 0 else ''
    EOK = 1e8
    JO  = 1e12
    if abs_won >= JO:
        jo  = int(abs_won // JO)
        eok = int((abs_won % JO) // EOK)
        return sign + (f'{jo}조 {eok:,}억' if eok else f'{jo}조')
    if abs_won >= EOK:
        return sign + f'{round(abs_won / EOK):,}억'
    eok_val = round(abs_won / EOK, 1)
    return sign + f'{eok_val}억' if eok_val else '—'


def fmt_net(val: Optional[int | float], unit: str = '백만원') -> str:
    """
    순매수 금액 포맷 (단위: 백만원 기본) → 조/억 표시 (JS fmtNet 동일)
    예: 1_500_000 → '+1.5조'  / 123_400 → '+1,234억'
    """
    if val is None:
        return '—'
    sign = '+' if val >= 0 else '-'
    a = abs(val)
    if a >= 1_000_000:
        return f'{sign}{a / 1_000_000:.1f}조'
    if a >= 100:
        return f'{sign}{round(a / 100):,}억'
    if a >= 1:
        return f'{sign}{a / 100:.1f}억'
    return f'{sign}{a}백만'


def fmt_money(val_100m: Optional[int], short: bool = False) -> str:
    """
    억 단위 입력 → 조/억 표시 (stock_api.format_money 동일 로직)
    예: 15000 → '1조 5,000억'
    """
    if val_100m is None:
        return '—'
    a = abs(val_100m)
    sign = '-' if val_100m < 0 else ''
    if a >= 10000:
        jo  = a // 10000
        rem = a % 10000
        if short:
            return f'{sign}{jo}조'
        return sign + (f'{jo}조 {rem:,}억' if rem else f'{jo}조')
    return f'{sign}{a:,}억'


def fmt_volume(val: Optional[int]) -> str:
    """거래량 → 만 단위 (stock_api.format_volume 동일)"""
    if val is None:
        return '—'
    if abs(val) >= 10000:
        return f'{val / 10000:.1f}만'
    return str(val)


# ── 날짜 ─────────────────────────────────────────────────────────────────────

def parse_date(val: str) -> Optional[str]:
    """
    YYYYMMDD → YYYY-MM-DD 변환. 빈값/오류면 None.
    collect_market.py / collect_financials.py 등에서 중복 사용하는 로직 통합.
    """
    if not val or len(str(val)) != 8:
        return None
    s = str(val)
    if not s.isdigit():
        return None
    return f'{s[:4]}-{s[4:6]}-{s[6:]}'


def yyyymmdd(dt) -> str:
    """date/datetime → 'YYYYMMDD' 문자열"""
    if hasattr(dt, 'strftime'):
        return dt.strftime('%Y%m%d')
    return str(dt).replace('-', '')[:8]


# ── 분기 ─────────────────────────────────────────────────────────────────────

def get_prev_quarter(year: str, quarter: str) -> tuple[str, str]:
    """
    직전 분기 반환. collect_financials.py와 grade.py에서 중복 정의된 함수 통합.
    예: ('2025', 'Q4') → ('2025', 'Q3')
        ('2025', 'Q1') → ('2024', 'Q4')
    """
    q_map = {'Q1': ('Q4', -1), 'Q2': ('Q1', 0), 'Q3': ('Q2', 0), 'Q4': ('Q3', 0)}
    prev_q, year_offset = q_map.get(quarter, ('Q4', -1))
    prev_y = str(int(year) + year_offset)
    return prev_y, prev_q
