"""
collect_financials.py
─────────────────────
DART OpenAPI → Supabase financials 테이블 수집기

실행:
    python collect_financials.py                  # 최근 분기 자동 감지
    python collect_financials.py --year 2024 --quarter Q4  # 직접 지정
    python collect_financials.py --all-listed      # 전체 상장사

스케줄 (run_all.py에서):
    토요일 새벽 2시 — 분기 보고서 제출 시즌에 자동 실행
"""

import os
import sys
import time
from logger_config import get_logger
log = get_logger(__name__)

import argparse
from datetime import datetime
from typing import Optional
from collect_utils import batch_upsert
from format_utils import get_prev_quarter

try:
    from db_client import get_supabase_client as _get_sb
except ImportError:
    from supabase import create_client as _cs
    def _get_sb():
        return _cs(os.getenv("SB_URL",""), os.getenv("SB_SERVICE_KEY",""))

try:
    import OpenDartReader
except ImportError:
    print("pip install opendartreader 필요")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

# supabase_bridge 활용 (페이지네이션 공통 헬퍼)
try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except Exception:
    _bridge = None
    _BRIDGE_OK = False

# ── 설정 ──
DART_API_KEY   = os.getenv("DART_API_KEY", "")
SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")

# ── 계정과목 패턴 매핑 ──────────────────────────────────────────
# 정확 일치(ACCOUNT_MAP) + 패턴 매칭(ACCOUNT_PATTERNS) 두 단계로 처리
# 1단계: 정확 일치 → 빠르고 명확
# 2단계: 패턴 매칭 → 회사별 변형 계정명 처리
# ────────────────────────────────────────────────────────────────

# 1단계: 정확 일치 맵
ACCOUNT_MAP = {
    # 매출
    "매출액":                    "revenue",
    "수익(매출액)":               "revenue",
    "영업수익":                   "revenue",
    "I. 영업수익":                "revenue",   # 솔트룩스, 루닛 등 번호 붙은 형태
    "매출":                      "revenue",   # 오뚜기, 젬백스 등
    "수익":                      "revenue",   # 일부 종목
    "매출액 및 지분법손익":          "revenue",   # 솔브레인홀딩스 등 지주사
    "순이자이익":                  "revenue",   # 은행/금융지주 전용
    "보험료수익":                  "revenue",   # 보험사
    "순수수료이익":                 "revenue",   # 증권사
    "영업수익(매출액)":             "revenue",   # 일부 변형
    # 영업이익 — 이익/손실/손익 모든 변형
    "매출총이익":                  "gross_profit",
    "매출총손실":                  "gross_profit",
    "매출총이익(손실)":             "gross_profit",
    "매출원가":                   "cogs",
    "제품매출원가":                 "cogs",
    "상품매출원가":                 "cogs",
    "판매비와관리비":               "sga",
    "판매비및관리비":               "sga",
    "판매비와 관리비":              "sga",   # 공백 변형
    "판매관리비":                  "sga",
    # "영업비용": sga 제거 — 영업비용은 매출원가+판관비+R&D 합계 계정이라 이중계산됨
    "연구개발비":                  "rd_expense",   # R&D 별도 계정
    "경상개발비":                  "rd_expense",   # 한미약품 등
    "연구개발비용":                 "rd_expense",   # 변형
    "연구비":                     "rd_expense",   # 일부 기업
    "기타영업수익":                 "other_operating_income",
    "기타영업외수익":               "other_operating_income",
    "기타영업수익(비용)":            "other_operating_income",
    "기타영업비용":                 "other_operating_expense",
    "기타영업외비용":               "other_operating_expense",
    "영업이익(손실)":              "operating_profit",
    "영업손익":                   "operating_profit",
    "영업손실":                   "operating_profit",   # 적자 기업
    "III. 영업손실":              "operating_profit",   # 솔트룩스 번호 붙은 형태
    "III. 영업이익":              "operating_profit",
    # 순이익
    "당기순이익":                  "net_income",
    "당기순이익(손실)":             "net_income",
    "당기순손실":                  "net_income",
    "당기순손익":                  "net_income",
    "분기순이익":                  "net_income",
    "분기순이익(손실)":             "net_income",
    "분기순손실":                  "net_income",
    "분기순손익":                  "net_income",
    "분기순수익(손실)":             "net_income",
    "분기손이익(손실)":             "net_income",
    "반기순이익":                  "net_income",
    "반기순이익(손실)":             "net_income",
    "반기순손실":                  "net_income",
    "반기순손익":                  "net_income",
    "연결당기순이익":               "net_income",
    "연결당기순이익(손실)":          "net_income",
    "연결당기순손실":               "net_income",
    "연결분기순이익":               "net_income",
    "연결분기순이익(손실)":          "net_income",
    "연결분기순손실":               "net_income",
    "연결반기순이익":               "net_income",
    "연결반기순이익(손실)":          "net_income",
    "연결반기순손실":               "net_income",
    "VI. 반기순이이익":             "net_income",   # 원익홀딩스 오타
    "분기손이익(순실)":             "net_income",   # 에코프로비엠 오타
    "보통주당기순이익":             "net_income",   # 디어유
    "보통주분기순이익":             "net_income",
    "보통주반기순이익":             "net_income",
    "지배주주당기순이익":             "net_income",   # 피에스케이 등
    "지배주주분기순이익":             "net_income",
    "지배주주반기순이익":             "net_income",
    "지배주주당기순이익(손실)":        "net_income",
    "지배주주분기순이익(손실)":        "net_income",
    "지배주주반기순이익(손실)":        "net_income",
    # 세전이익
    "법인세비용차감전순이익":         "pretax_income",
    "법인세비용차감전순이익(손실)":    "pretax_income",
    "법인세비용차감전분기순이익(손실)": "pretax_income",
    "법인세비용차감전반기순이익(손실)": "pretax_income",
    "법인세차감전순손실":            "pretax_income",   # 제이엘케이 등
    "법인세차감전순이익":            "pretax_income",
    "법인세차감전순손익":            "pretax_income",
    "법인세차감전이익":              "pretax_income",   # "순"자 없는 변형
    "법인세차감전손실":              "pretax_income",
    "법인세차감전이익(손실)":         "pretax_income",
    # 재무상태표
    "자산총계":                   "total_assets",
    "자산 합계":                  "total_assets",
    "자산합계":                   "total_assets",
    "부채총계":                   "total_liabilities",
    "부채 합계":                  "total_liabilities",
    "부채합계":                   "total_liabilities",
    "자본총계":                   "total_equity",
    "자본 합계":                  "total_equity",
    "자본합계":                   "total_equity",
    "유동자산":                   "current_assets",
    "유동자산합계":                 "current_assets",
    "유동자산 합계":               "current_assets",
    "유동부채":                   "current_liabilities",
    "유동부채합계":                 "current_liabilities",
    "유동부채 합계":               "current_liabilities",
    "비유동자산":                  "non_current_assets",
    "비유동자산합계":               "non_current_assets",
    "비유동자산 합계":              "non_current_assets",
    "자본금":                    "capital_stock",
    "이익잉여금":                  "retained_earnings",
    "이익잉여금(결손금)":            "retained_earnings",   # 적자 기업
    "미처분이익잉여금":              "retained_earnings",
    # 현금흐름
    "영업활동 현금흐름":             "operating_cashflow",
    "영업활동으로 인한 현금흐름":      "operating_cashflow",
    "영업활동현금흐름":              "operating_cashflow",
    "투자활동 현금흐름":             "investing_cashflow",
    "투자활동으로 인한 현금흐름":      "investing_cashflow",
    "투자활동현금흐름":              "investing_cashflow",
    "재무활동 현금흐름":             "financing_cashflow",
    "재무활동으로 인한 현금흐름":      "financing_cashflow",
    "재무활동현금흐름":              "financing_cashflow",
    # CapEx (유형자산 취득) — FCF 계산용
    "유형자산의 취득":               "capex",
    "유형자산취득":                  "capex",
    "유형자산의취득":                "capex",
    "유형자산 취득":                 "capex",
    "유형자산의 구입":               "capex",
    "유형자산 구입":                 "capex",
    "유형자산의 증가":               "capex",
    "유형자산 증가":                 "capex",
    "유형자산취득으로인한현금유출":     "capex",
    # CapEx (무형자산 취득) — FCF 계산용 확장
    "무형자산의 취득":               "capex_intangible",
    "무형자산취득":                  "capex_intangible",
    "무형자산 취득":                 "capex_intangible",
    "무형자산의취득":                "capex_intangible",
    "소프트웨어의 취득":             "capex_intangible",
    "소프트웨어취득":                "capex_intangible",
    "소프트웨어의취득":              "capex_intangible",
    "개발비의 취득":                 "capex_intangible",
    "개발비취득":                   "capex_intangible",
    # D&A (감가상각비) — 현금흐름표 영업활동 조정항목 (누적값)
    "감가상각비":                   "depreciation",
    "유형자산감가상각비":            "depreciation",
    "유형자산 감가상각비":           "depreciation",
    "사용권자산감가상각비":          "depreciation",
    "사용권자산 감가상각비":         "depreciation",
    "투자부동산감가상각비":          "depreciation",   # 일부 금융·부동산사
    "감가상각비및상각비":            "depreciation",   # D&A 합산 계정 (일부 기업)
    "무형자산상각비":               "amortization",
    "무형자산 상각비":              "amortization",
    "무형자산의상각":               "amortization",
    "무형자산의 상각":              "amortization",
    "개발비상각":                   "amortization",
    "개발비 상각":                  "amortization",
    "영업권상각비":                 "amortization",   # 일부 기업
}

# 2단계: 패턴 매칭 (정확 일치 실패 시 순서대로 적용)
# (포함 키워드 리스트, 제외 키워드 리스트, DB 컬럼)
ACCOUNT_PATTERNS = [
    # 매출 — "매출액" 또는 단순 "매출" (채권/원가/총이익 제외)
    (["수익(매출액)"],                [],                              "revenue"),
    (["매출액"],                     ["채권","원가","총이익","기타"],    "revenue"),
    (["매출"],                       ["채권","원가","총이익","기타","비유동"], "revenue"),
    # 영업이익 — 이익/손익/손실 모든 변형
    (["매출원가"],                   ["채권","총이익","비유동"],        "cogs"),
    (["판매비","관리비"],             ["주당","비율"],                   "sga"),
    (["판관비"],                     ["주당","비율"],                   "sga"),
    (["영업이익"],                   ["외","수익","비용","현금"],   "operating_profit"),
    (["영업손익"],                   ["외","수익","비용","현금"],   "operating_profit"),
    (["영업손실"],                   ["외","수익","비용","현금"],   "operating_profit"),
    # 순이익 — 손실/손익/손 모든 변형
    (["계속영업", "순이익"],          ["비용","세","주당","법인"],   "net_income"),
    (["계속영업", "순손익"],          ["비용","세","주당","법인"],   "net_income"),
    (["계속영업", "순손"],            ["비용","세","주당","법인","귀속"], "net_income"),
    (["계속영업", "귀속"],            ["비용","세","주당","법인"],   "net_income"),
    (["분기순이익"],                  ["비지배","주당"],            "net_income"),
    (["분기순손실"],                  ["비지배","주당"],            "net_income"),
    (["분기순손익"],                  ["비지배","주당"],            "net_income"),
    (["반기순이익"],                  ["비지배","주당"],            "net_income"),
    (["반기순손실"],                  ["비지배","주당"],            "net_income"),
    (["반기순손익"],                  ["비지배","주당"],            "net_income"),
    (["당기순이익"],                  ["비지배","주당"],            "net_income"),
    (["당기순손실"],                  ["비지배","주당"],            "net_income"),
    (["당기순손익"],                  ["비지배","주당"],            "net_income"),
    (["연결", "순이익"],              ["비지배","주당"],            "net_income"),
    (["연결", "순손익"],              ["비지배","주당"],            "net_income"),
    (["연결", "순손실"],              ["비지배","주당"],            "net_income"),
    (["지배주주", "순이익"],           ["비지배","주당","포괄"],      "net_income"),
    (["지배주주", "순손실"],           ["비지배","주당","포괄"],      "net_income"),
    (["지배주주", "순손익"],           ["비지배","주당","포괄"],      "net_income"),
    # 세전이익
    (["법인세비용차감전", "순이익"],    ["주당"],                    "pretax_income"),
    (["법인세비용차감전", "순손익"],    ["주당"],                    "pretax_income"),
    # 재무상태표
    (["자산총계"],                   [],                         "total_assets"),
    (["자산합계"],                   [],                         "total_assets"),
    (["부채총계"],                   [],                         "total_liabilities"),
    (["부채합계"],                   [],                         "total_liabilities"),
    (["자본총계"],                   [],                         "total_equity"),
    (["자본합계"],                   [],                         "total_equity"),
    (["이익잉여금"],                  ["처분"],                    "retained_earnings"),
    # 현금흐름 — "영업/투자/재무" + "현금흐름" 포함
    (["영업활동", "현금흐름"],         [],                         "operating_cashflow"),
    (["영업활동", "현금"],            ["유출","유입","창출","변동"],  "operating_cashflow"),
    (["투자활동", "현금흐름"],         [],                         "investing_cashflow"),
    (["투자활동", "현금"],            ["유출","유입"],              "investing_cashflow"),
    (["재무활동", "현금흐름"],         [],                         "financing_cashflow"),
    (["재무활동", "현금"],            ["유출","유입"],              "financing_cashflow"),
    # CapEx 패턴 (투자활동 하위 계정 — 유형자산 취득)
    (["유형자산", "취득"],             ["처분","대체","감소","손상"],  "capex"),
    (["유형자산", "구입"],             ["처분","대체"],               "capex"),
    (["유형자산", "증가"],             ["처분","감소","손상","재평가"],"capex"),
    # CapEx 무형자산 패턴 (투자활동 하위 계정)
    (["무형자산", "취득"],             ["처분","손상","감소","재평가"], "capex_intangible"),
    (["소프트웨어", "취득"],           ["처분","감소"],               "capex_intangible"),
    (["개발비", "취득"],              ["처분","감소"],               "capex_intangible"),
    # D&A 패턴 (현금흐름표 영업활동 조정항목)
    (["감가상각비"],                   ["손실","손상","누계","처분","평가","비유동","계정"],  "depreciation"),
    (["감가상각"],                     ["손실","손상","누계","처분","평가","비유동","계정"],  "depreciation"),
    (["사용권자산", "감가상각"],        ["처분","손상","누계"],                              "depreciation"),
    (["무형자산", "상각비"],           ["손상","처분","환입","재평가","누계"],               "amortization"),
    (["무형자산", "상각"],             ["손상","처분","환입","재평가","누계","취득"],         "amortization"),
    (["개발비", "상각"],              ["취득","처분"],                                     "amortization"),
]


def resolve_account(acct: str) -> Optional[str]:
    """
    계정명 → DB 컬럼 변환.
    1단계: ACCOUNT_MAP 정확 일치
    2단계: ACCOUNT_PATTERNS 패턴 매칭
    """
    # 1단계: 정확 일치
    col = ACCOUNT_MAP.get(acct)
    if col:
        return col

    # 2단계: 패턴 매칭
    for includes, excludes, target_col in ACCOUNT_PATTERNS:
        if all(kw in acct for kw in includes):
            if not any(kw in acct for kw in excludes):
                return target_col

    return None

# 분기별 보고서 타입
QUARTER_REPORT = {
    "Q1": ("11013", "분기보고서"),   # 1분기
    "Q2": ("11012", "반기보고서"),   # 반기
    "Q3": ("11014", "분기보고서"),   # 3분기
    "Q4": ("11011", "사업보고서"),   # 연간
    "FY": ("11011", "사업보고서"),
}

# ── 누적→단독 변환 대상 컬럼 ──
# 손익계산서: Q1/Q2/Q3는 단독값, Q4만 연간누적 → Q4만 변환
INCOME_COLS = [
    "revenue", "gross_profit", "cogs", "sga", "rd_expense",
    "other_operating_income", "other_operating_expense",
    "operating_profit", "net_income", "pretax_income",
]
# 현금흐름: Q1~Q4 모두 누적값 → 항상 변환 (Q1 제외)
CASHFLOW_COLS = [
    "operating_cashflow", "investing_cashflow", "financing_cashflow",
    "capex",            # 유형자산취득 (투자활동 하위 — 누적값 변환 필요)
    "capex_intangible", # 무형자산취득 (투자활동 하위 — 누적값 변환 필요)
    "depreciation",     # 감가상각비 (영업활동 조정항목 — 누적값 변환 필요)
    "amortization",     # 무형자산상각비 (영업활동 조정항목 — 누적값 변환 필요)
]
# 재무상태표: 특정 시점 잔액 → 변환 불필요
# total_assets, total_liabilities, total_equity 등

# 전체 FLOW_COLS (캐시 저장용)
FLOW_COLS = INCOME_COLS + CASHFLOW_COLS


def is_cumulative_by_comparison(cur_val: int, prev_val: int) -> bool:
    """
    Q2/Q3 값이 누적인지 단독인지 자동 판별.
    cur_val이 prev_val의 1.8배 이상이면 누적으로 판단.
    금융사처럼 일부 업종이 누적값을 제공하는 경우 대비.
    """
    if cur_val is None or prev_val is None or prev_val == 0:
        return False
    return cur_val > prev_val * 1.8


def get_q3_cumulative(dart, corp_code: str, year: str, fs_div: str = "CFS") -> Optional[dict]:
    """
    Q4 변환용 Q3 누적값을 DART 3분기보고서에서 직접 조회.
    분기보고서에는 두 가지 금액 컬럼이 있음:
      - thstrm_amount    : 당분기(Q3 단독값)
      - thstrm_add_amount: 당기누적(Q1+Q2+Q3 누적값) ← Q4 변환에 필요한 값
    """
    try:
        df = dart.finstate_all(
            corp_code, bsns_year=year,
            reprt_code="11014",  # 3분기보고서
            fs_div=fs_div
        )
        if df is None:
            return None
        result = {}
        for _, r in df.iterrows():
            acct = str(r.get("account_nm", "")).strip()
            col  = resolve_account(acct)
            if col and col in FLOW_COLS and col not in result:
                # thstrm_add_amount(누적) 우선, 없으면 thstrm_amount(단독) 사용
                amt = parse_amount(str(r.get("thstrm_add_amount", "")))
                if amt is None:
                    amt = parse_amount(str(r.get("thstrm_amount", "")))
                if amt is not None:
                    result[col] = amt
        return result if result else None
    except Exception as e:
        log.warning(f"[Q3누적조회실패] {corp_code} {year}: {e}")
        return None


def convert_to_pure_quarter(row: dict, prev_row: Optional[dict]) -> dict:
    """
    DART 제공 방식:
    - 손익계산서 (revenue, gross_profit, operating_profit, net_income, pretax_income):
        Q1/Q2/Q3 → 단독값 제공 (대부분), Q4 → 연간 누적
        Q2/Q3는 revenue 기준 자동 판별 후 누적이면 변환
    - 현금흐름 (operating/investing/financing_cashflow):
        Q1~Q4 모두 누적값 제공 → Q2/Q3/Q4 항상 변환
    - 재무상태표 (total_assets 등):
        특정 시점 잔액 → 변환 불필요
    """
    quarter = row.get("quarter", "Q1")
    converted = dict(row)

    # Q1: 손익은 단독, 현금흐름도 단독 → 전부 그대로
    if quarter == "Q1":
        converted["is_cumulative"] = False
        return converted

    if prev_row is None:
        log.warning(
            f"[누적변환] {row.get('corp_name')} {row.get('bsns_year')} {quarter} — "
            f"이전 분기 데이터 없음, 누적값 그대로 저장 (is_cumulative=True)"
        )
        converted["is_cumulative"] = True
        return converted

    # ── 현금흐름: Q2/Q3/Q4 항상 누적 → 변환 ──
    for col in CASHFLOW_COLS:
        cur  = row.get(col)
        prev = prev_row.get(col)
        if cur is not None and prev is not None:
            converted[col] = cur - prev

    # ── 손익계산서 ──
    if quarter == "Q4":
        # Q4: 연간 누적 → Q3 빼서 단독값
        for col in INCOME_COLS:
            cur  = row.get(col)
            prev = prev_row.get(col)
            if cur is not None and prev is not None:
                converted[col] = cur - prev
    else:
        # Q2/Q3: DART thstrm_amount는 당해 단독값 → 변환 불필요
        # (일부 금융사 누적 제공 케이스는 thstrm_amount도 단독값 제공)
        pass

    converted["is_cumulative"] = False
    return converted



def parse_amount(val: str) -> Optional[int]:
    """문자열 금액 → 정수 (단위: 원)"""
    if not val or val.strip() in ("", "-", "－"):
        return None
    try:
        cleaned = val.replace(",", "").replace(" ", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        return int(cleaned)
    except (ValueError, AttributeError):
        return None


def calc_ratios(row: dict) -> dict:
    """재무 비율 계산 + 매출원가/판관비 교차 검증"""
    rev  = row.get("revenue")
    op   = row.get("operating_profit")
    gp   = row.get("gross_profit")
    cogs = row.get("cogs")
    sga  = row.get("sga")
    ta   = row.get("total_assets")
    tl   = row.get("total_liabilities")
    te   = row.get("total_equity")
    cl   = row.get("current_liabilities")
    ca   = row.get("current_assets")
    ni   = row.get("net_income")

    result = {}
    corp  = row.get("corp_name", "")
    year  = row.get("bsns_year", "")
    qtr   = row.get("quarter", "")

    try:
        # ── 매출원가/매출총이익 역산 ──
        if rev and rev != 0:
            # 매출원가 음수면 절댓값 사용
            if cogs is not None and cogs < 0:
                cogs = abs(cogs)
                result["cogs"] = cogs
            if cogs is None and gp is not None:
                # 매출원가 역산
                cogs = rev - gp
                if cogs > 0:
                    result["cogs"] = cogs
            elif cogs is not None and gp is None:
                # 매출총이익 역산
                if cogs > 0:
                    gp = rev - cogs
                    result["gross_profit"] = gp

        # ── 판관비 역산 ──
        # gross_profit 없는 구조(영업수익-영업비용)는 스킵
        if gp is None:
            pass
        elif op is not None and sga is None:
            rd  = row.get("rd_expense") or result.get("rd_expense") or 0
            ooi = row.get("other_operating_income") or 0
            ooe = row.get("other_operating_expense") or 0
            sga_calc = gp - rd + ooi - ooe - op
            if sga_calc > 0:
                result["sga"] = sga_calc
                log.debug(f"[역산] {corp} {year} {qtr} 판관비 = {sga_calc//int(1e8)}억")

        # ── 비율 계산 ──
        if rev and rev != 0:
            cogs_final = cogs if cogs is not None else result.get("cogs")
            gp_final   = gp   if gp   is not None else result.get("gross_profit")
            sga_final  = sga  if sga  is not None else result.get("sga")

            if cogs_final is not None:
                result["cogs_ratio"]   = round(cogs_final / rev * 100, 2)
            if gp_final is not None:
                result["gross_margin"] = round(gp_final   / rev * 100, 2)
            if sga_final is not None:
                result["sga_ratio"]    = round(sga_final  / rev * 100, 2)
            if ni is not None:
                result["net_margin"]   = round(ni          / rev * 100, 2)
            if op is not None:
                result["operating_margin"] = round(op      / rev * 100, 2)

        if te and tl and te != 0:
            result["debt_ratio"]    = round(tl / te * 100, 2)
        if cl and ca and cl != 0:
            result["current_ratio"] = round(ca / cl * 100, 2)
        if te and ni and te != 0:
            result["roe"] = round(ni / te * 100, 2)
        if ta and ni and ta != 0:
            result["roa"] = round(ni / ta * 100, 2)

        # ── D&A / CapEx 합계 / EBITDA / FCF ──
        ocf         = row.get("operating_cashflow")
        capex_tan   = row.get("capex")           # 유형자산취득
        capex_int   = row.get("capex_intangible")# 무형자산취득
        dep         = row.get("depreciation")    # 감가상각비
        amo         = row.get("amortization")    # 무형자산상각비

        # D&A 합계 (어느 하나라도 있으면 계산)
        da = None
        if dep is not None or amo is not None:
            da = (dep or 0) + (amo or 0)
            if da > 0:
                result["da"] = da

        # CapEx 합계 (유형+무형, DART 음수 표기 → 절댓값)
        capex_total = None
        if capex_tan is not None or capex_int is not None:
            capex_total = abs(capex_tan or 0) + abs(capex_int or 0)
            result["capex_total"] = capex_total

        # EBITDA = 영업이익 + D&A
        if op is not None and da is not None:
            result["ebitda"] = op + da

        # FCF 직접법 = OCF - CapEx 합계  (가장 신뢰도 높음)
        fcf_direct = None
        if ocf is not None and capex_total is not None:
            fcf_direct = ocf - capex_total
            result["fcf_direct"] = fcf_direct
        elif ocf is not None and capex_tan is not None:
            # capex_intangible 없을 때 유형만으로 계산
            fcf_direct = ocf - abs(capex_tan)
            result["fcf_direct"] = fcf_direct

        # FCF 간접법 = 순이익 + D&A - CapEx 합계  (OCF 없는 분기 폴백)
        fcf_indirect = None
        if ni is not None and da is not None and capex_total is not None:
            fcf_indirect = ni + da - capex_total
            result["fcf_indirect"] = fcf_indirect

        # FCF 최선값: 직접법 → 간접법 → OCF 근사 순
        if fcf_direct is not None:
            result["fcf"] = fcf_direct
        elif fcf_indirect is not None:
            result["fcf"] = fcf_indirect
        elif ocf is not None:
            result["fcf"] = ocf  # CapEx 없음 — OCF 근사 (보수적)

    except Exception as e:
        log.debug(f"calc_ratios 오류 {corp}: {e}")
    return result


def calc_growth(new_val, old_val) -> Optional[float]:
    """성장률 계산 (%). old_val이 0이거나 None이면 None 반환"""
    try:
        if new_val is None or old_val is None or old_val == 0:
            return None
        return round((new_val - old_val) / abs(old_val) * 100, 2)
    except Exception:
        return None


def get_prev_year_quarter(year: str, quarter: str) -> tuple:
    """전년 동기 반환 (year, quarter)"""
    return str(int(year) - 1), quarter


def build_fin_cache(sb) -> dict:
    """
    기존 financials 데이터를 메모리에 로드.
    - 성장률 계산용 + 누적→단독 변환용으로 사용
    캐시 구조: {stock_code: {(year, quarter): {field: value}}}
    """
    cache = {}
    page = 0
    select_cols = "stock_code,bsns_year,quarter,fs_div," + ",".join(FLOW_COLS)
    while True:
        res = sb.table("financials") \
            .select(select_cols) \
            .range(page * 1000, (page + 1) * 1000 - 1) \
            .execute()
        if not res.data:
            break
        for r in res.data:
            code = r["stock_code"]
            key  = (r["bsns_year"], r["quarter"], r.get("fs_div", "CFS"))
            if code not in cache:
                cache[code] = {}
            cache[code][key] = {col: r.get(col) for col in FLOW_COLS}
        if len(res.data) < 1000:
            break
        page += 1
    return cache


def get_pure_val_from_cache(cache: dict, stock_code: str, year: str, quarter: str,
                            field: str, fs_div: str = "CFS") -> Optional[int]:
    """캐시에서 순수 분기값 반환 (DB에 이미 단독값으로 저장된 값 기준)"""
    stock_cache = cache.get(stock_code, {})
    return stock_cache.get((year, quarter, fs_div), {}).get(field)


def calculate_growth_rates(cache: dict, row: dict) -> dict:
    """
    캐시 기반 YoY/QoQ 성장률 계산 (DB 추가 쿼리 없음)
    DB에 이미 순수 분기값이 저장되어 있으므로 캐시값을 직접 비교
    """
    stock_code = row['stock_code']
    year       = row['bsns_year']
    quarter    = row['quarter']
    fs_div     = row.get('fs_div', 'CFS')
    growth     = {}

    fields = [
        ('revenue',          'revenue_yoy',     'revenue_qoq'),
        ('operating_profit', 'op_profit_yoy',   'op_profit_qoq'),
        ('net_income',       'net_income_yoy',  'net_income_qoq'),
    ]

    # YoY: 전년 동기 순수 분기값과 비교 (캐시에 이미 순수값 저장)
    prev_y, prev_q_yoy = get_prev_year_quarter(year, quarter)
    yoy_data = cache.get(stock_code, {}).get((prev_y, prev_q_yoy, fs_div), {})
    for src, yoy_col, _ in fields:
        growth[yoy_col] = calc_growth(row.get(src), yoy_data.get(src))

    # QoQ: 이전 분기 순수값과 비교
    prev_y_q, prev_q = get_prev_quarter(year, quarter)
    prev_pure = {}
    if prev_y_q and prev_q:
        prev_pure = cache.get(stock_code, {}).get((prev_y_q, prev_q, fs_div), {})

    for src, _, qoq_col in fields:
        growth[qoq_col] = calc_growth(row.get(src), prev_pure.get(src))

    return growth


def collect_one(dart, corp_code: str, stock_code: str, corp_name: str,
                year: str, quarter: str) -> Optional[dict]:
    """단일 종목 재무제표 수집"""
    report_code, report_type = QUARTER_REPORT.get(quarter, ("11011", "사업보고서"))

    try:
        # 연결재무제표 우선, 없으면 별도재무제표
        for fs_div in ["CFS", "OFS"]:
            try:
                df = dart.finstate_all(
                    corp_code,
                    bsns_year=year,
                    reprt_code=report_code,
                    fs_div=fs_div
                )
                if df is not None and not df.empty:
                    break
            except Exception:
                df = None

        if df is None or df.empty:
            log.debug(f"[SKIP] {corp_name}: 데이터 없음")
            return None

        row = {
            "corp_code":   corp_code,
            "stock_code":  stock_code.split(".")[0],
            "corp_name":   corp_name,
            "bsns_year":   year,
            "quarter":     quarter,
            "report_type": report_type,
            "fs_div":      fs_div,
            "currency":    "KRW",
        }

        # 계정과목별 금액 추출
        # Q1/Q2/Q3: thstrm_amount = 당해 단독값 → 그대로 사용
        # Q4(사업보고서): thstrm_amount = 연간 누적값 → 이후 convert_to_pure_quarter에서 변환
        # revenue는 합계 계정 우선 — 세부항목(재화판매, 용역 등)이 먼저 나와도 합계로 덮어씀
        # revenue 우선순위: 정확한 계정명 순서로 선택
        REVENUE_PRIORITY = ['매출액', '수익(매출액)', '영업수익', '매출', '수익',
                           '순매출액', '매출액 및 지분법손익', '총매출액', '영업수익(매출액)']
        candidate = {}

        for _, r in df.iterrows():
            acct = str(r.get("account_nm", "")).strip()
            col  = resolve_account(acct)
            if not col:
                continue
            amt = parse_amount(str(r.get("thstrm_amount", "")))
            if amt is None:
                continue

            if col == 'revenue':
                # revenue는 우선순위 높은 계정명으로 교체
                if col not in candidate:
                    candidate[col] = (amt, acct)
                else:
                    cur_priority  = REVENUE_PRIORITY.index(candidate[col][1]) if candidate[col][1] in REVENUE_PRIORITY else 999
                    new_priority  = REVENUE_PRIORITY.index(acct) if acct in REVENUE_PRIORITY else 999
                    if new_priority < cur_priority:
                        log.debug(f"[우선순위] {corp_name} revenue: {candidate[col][1]} → {acct}")
                        candidate[col] = (amt, acct)
            elif col not in candidate:
                # 첫 등장: 0이라도 일단 저장 (DART에서 실제 0 보고한 경우)
                candidate[col] = (amt, acct)
            elif candidate[col][0] == 0 and amt != 0:
                # 기존에 0이 저장됐는데 뒤에 실제값(비-0)이 나오면 교체
                log.debug(f"[0→실제값] {corp_name} {col}: 0 → {amt}")
                candidate[col] = (amt, acct)

        for col, (amt, _) in candidate.items():
            row[col] = amt

        # 파생 비율 계산
        # net_income이 없으면 pretax_income으로 fallback (제이엘케이 등 일부 기업)
        if row.get('net_income') is None and row.get('pretax_income') is not None:
            row['net_income'] = row['pretax_income']
            log.debug(f"[net_income fallback] {corp_name}: pretax_income → net_income")

        row.update(calc_ratios(row))

        return row

    except Exception as e:
        log.error(f"[ERROR] {corp_name} ({corp_code}): {e}")
        return None


def run_by_corp_codes(corp_codes: list, year: str, quarter: str, max_workers: int = 3):
    """
    특정 corp_code 목록만 재무 수집 — DART 오늘 공시 기반 수집용
    returns: (ok_count, fail_count)
    """
    if not corp_codes:
        return 0, 0

    dart = OpenDartReader(DART_API_KEY)
    sb   = _get_sb()

    log.info(f"=== 공시 기반 재무 수집: {year} {quarter} — {len(corp_codes)}개 종목 ===")

    # companies 테이블에서 해당 corp_code 종목 정보 조회
    targets = []
    for i in range(0, len(corp_codes), 100):
        chunk = corp_codes[i:i+100]
        res = sb.table('companies').select('code,name,corp_code') \
            .in_('corp_code', chunk).eq('active', True).execute()
        targets.extend(res.data or [])

    # DB에 없는 corp_code는 DART에서 직접 corp_name 조회
    found_codes = {r['corp_code'] for r in targets}
    missing = [c for c in corp_codes if c not in found_codes]
    for corp_code in missing:
        try:
            info = dart.company(corp_code)
            if info is not None:
                targets.append({
                    'corp_code': corp_code,
                    'code':      info.get('stock_code', ''),
                    'name':      info.get('corp_name', corp_code),
                })
        except:
            pass

    if not targets:
        log.warning("수집 대상 없음")
        return 0, 0


    ok, fail = 0, 0
    collected = []
    for i, t in enumerate(targets, 1):
        corp_code  = t.get('corp_code', '')
        stock_code = (t.get('code', '') or '').replace('.KS', '').replace('.KQ', '')
        corp_name  = t.get('name', corp_code)
        for attempt in range(2):  # 최대 2회 시도 (원본 + 1회 retry)
            try:
                log.info(f"[{i}/{len(targets)}] {corp_name}" + (" (재시도)" if attempt else ""))
                row = collect_one(dart, corp_code, stock_code, corp_name,
                                  year=year, quarter=quarter)
                if row:
                    # ── Q4 누적→단독 변환 ──────────────────────────
                    if quarter == "Q4":
                        fs_div_row = row.get("fs_div", "CFS")
                        prev_row = get_q3_cumulative(dart, corp_code, year, fs_div_row)
                        if prev_row:
                            log.info(f"[Q3누적조회] {corp_name} {year} Q4 변환 완료")
                        else:
                            # 폴백: DB에서 Q1+Q2+Q3 합산
                            log.warning(f"[Q3누적조회실패] {corp_name} {year} — DB 합산 폴백")
                            try:
                                _fb = sb.table("financials") \
                                    .select(",".join(["quarter","fs_div"] + FLOW_COLS)) \
                                    .eq("stock_code", stock_code) \
                                    .eq("bsns_year", year) \
                                    .in_("quarter", ["Q1","Q2","Q3"]) \
                                    .eq("fs_div", fs_div_row).execute()
                                if _fb.data and len(_fb.data) == 3:
                                    prev_row = {}
                                    for col in FLOW_COLS:
                                        vals = [r.get(col) for r in _fb.data if r.get(col) is not None]
                                        if vals:
                                            prev_row[col] = sum(vals)
                            except Exception as _fe:
                                log.warning(f"[Q4폴백오류] {corp_name}: {_fe}")
                        row = convert_to_pure_quarter(row, prev_row)
                    collected.append(row)
                ok += 1
                break
            except Exception as e:
                if attempt == 0:
                    log.warning(f"  ⚠️ {corp_name} 1차 실패, 2초 후 재시도: {e}")
                    time.sleep(2)
                else:
                    log.error(f"  ❌ {corp_name} 최종 실패: {e}")
                    fail += 1

    # DB upsert (50개씩 배치)
    if collected:
        # YoY/QoQ 계산을 위한 이전 분기 데이터 조회
        stock_codes = [r['stock_code'] for r in collected]
        prev_y_q, prev_q_val = get_prev_quarter(year, quarter)
        prev_year = str(int(year) - 1)

        fin_cache = {}  # {stock_code: {(year, quarter, fs_div): row}}
        try:
            # 전년동기 + 직전 분기 한번에 조회
            quarters_to_fetch = [(prev_year, quarter)]
            if prev_y_q and prev_q_val:
                quarters_to_fetch.append((prev_y_q, prev_q_val))

            for fy, fq in quarters_to_fetch:
                res = sb.table('financials') \
                    .select('stock_code,bsns_year,quarter,fs_div,revenue,operating_profit,net_income') \
                    .in_('stock_code', stock_codes) \
                    .eq('bsns_year', fy).eq('quarter', fq).execute()
                for r in (res.data or []):
                    sc = r['stock_code']
                    if sc not in fin_cache:
                        fin_cache[sc] = {}
                    fin_cache[sc][(r['bsns_year'], r['quarter'], r['fs_div'])] = r
        except Exception as e:
            log.warning(f"YoY/QoQ 캐시 조회 실패 (성장률 계산 스킵): {e}")

        # 성장률 계산 후 row 업데이트
        for row in collected:
            try:
                growth = calculate_growth_rates(fin_cache, row)
                row.update(growth)
            except Exception as e:
                log.debug(f"성장률 계산 실패 {row.get('corp_name')}: {e}")

        batch_upsert(sb, "financials", collected,
                     "corp_code,bsns_year,quarter,fs_div",
                     chunk=50, progress_label="DB 저장")

    log.info(f"=== 공시 기반 수집 완료: 성공 {ok} / 실패 {fail} ===")
    return ok, fail


def run_by_corp_codes_all_history(corp_codes: list, from_year: int = 2023, max_workers: int = 2):
    """
    신규 모니터링 종목의 과거 재무 데이터 일괄 수집.
    from_year부터 최신 분기까지 모든 분기를 수집.
    """
    if not corp_codes:
        return

    from datetime import datetime as _dt
    now = _dt.now()
    current_year = now.year

    # 수집할 (year, quarter) 목록 생성
    quarters_to_collect = []
    for y in range(from_year, current_year + 1):
        for q in ['Q1', 'Q2', 'Q3', 'Q4']:
            # 미래 분기 제외
            if y == current_year:
                month = now.month
                if q == 'Q1' and month < 5: continue
                if q == 'Q2' and month < 8: continue
                if q == 'Q3' and month < 11: continue
                if q == 'Q4': continue  # Q4는 다음해 3월 제출
            quarters_to_collect.append((str(y), q))

    log.info(f"💰 [재무 히스토리] {len(corp_codes)}개 종목 × {len(quarters_to_collect)}개 분기 수집 시작")
    log.info(f"   대상 기간: {quarters_to_collect[0]} ~ {quarters_to_collect[-1]}")

    ok_total = 0
    for year, quarter in quarters_to_collect:
        try:
            ok, fail = run_by_corp_codes(corp_codes, year=year, quarter=quarter, max_workers=max_workers)
            ok_total += ok
            log.info(f"  {year} {quarter}: {ok}개 성공, {fail}개 실패")
        except Exception as e:
            log.error(f"  {year} {quarter}: 오류 — {e}")

    log.info(f"💰 [재무 히스토리] 완료: 총 {ok_total}건 수집")


def auto_detect_quarter() -> tuple:
    """현재 날짜 기준 최근 제출된 분기 자동 감지"""
    now = datetime.now()
    month = now.month
    year  = str(now.year)

    # 보고서 제출 시기 기준
    if month <= 3:
        # 1~3월: 전년도 Q3 (11월 제출)
        return str(now.year - 1), "Q3"
    elif month <= 5:
        # 4~5월: 당해 Q1 (1분기보고서, 5월 제출)
        return year, "Q1"
    elif month <= 8:
        # 6~8월: Q2/반기 (8월 제출)
        return year, "Q2"
    elif month <= 11:
        # 9~11월: Q3 (11월 제출)
        return year, "Q3"
    else:
        # 12월: 전년도 Q4 준비, Q3 기준
        return year, "Q3"


def run(year: str, quarter: str, all_listed: bool = False, max_workers: int = 3,
        monitored_only: bool = False, non_monitored_only: bool = False,
        corp_names: list = None):
    """메인 수집 함수"""
    if not DART_API_KEY or not SB_URL or not SB_SERVICE_KEY:
        log.error("DART_API_KEY, SB_URL, SB_SERVICE_KEY 환경변수 필요")
        sys.exit(1)

    dart = OpenDartReader(DART_API_KEY)
    sb   = _get_sb()

    log.info(f"=== 재무 데이터 수집 시작: {year} {quarter} ===")

    # 수집 대상 결정
    if all_listed:
        # 전체 상장사: collect_listed_companies.py 역할과 중복이므로
        # bridge를 통해 DB에서 active 종목 전체 로드
        log.info("전체 상장사 모드 — companies 테이블 전체 (active=True)")
        if _BRIDGE_OK:
            raw_all = _bridge.fetch_all_pages(
                sb.table("companies").select("name, code").eq("active", True)
            )
        else:
            raw_all = []
            page = 0
            while True:
                res = sb.table("companies").select("name, code").eq("active", True) \
                    .range(page * 1000, (page + 1) * 1000 - 1).execute()
                if not res.data: break
                raw_all.extend(res.data)
                if len(res.data) < 1000: break
                page += 1
        targets_raw = raw_all
    else:
        # companies 테이블에서 로드
        q = sb.table("companies").select("name, code")
        if monitored_only:
            q = q.eq("is_monitored", True)
            log.info("모니터링 종목만 수집 (is_monitored=True)")
        elif non_monitored_only:
            q = q.eq("active", True).eq("is_monitored", False)
            log.info("비모니터링 종목만 수집 (is_monitored=False, active=True)")
        else:
            q = q.eq("active", True)

        if _BRIDGE_OK:
            targets_raw = _bridge.fetch_all_pages(q)
        else:
            targets_raw = []
            page = 0
            while True:
                res = q.range(page * 1000, (page + 1) * 1000 - 1).execute()
                if not res.data: break
                targets_raw.extend(res.data)
                if len(res.data) < 1000: break
                page += 1

        # DART 전체 기업 목록에서 corp_code 매핑 (종목코드 기준)
        # corp_names 필터 적용
        if corp_names:
            targets_raw = [t for t in targets_raw if t.get('name') in corp_names]
            log.info(f"종목명 필터 적용: {corp_names} → {len(targets_raw)}개")
        try:
            corp_df = dart.corp_codes  # 전체 기업 코드 DataFrame
            code_map = {}
            if corp_df is not None and not corp_df.empty:
                for _, row in corp_df.iterrows():
                    sc = str(row.get("stock_code", "")).strip()
                    cc = str(row.get("corp_code", "")).strip()
                    if sc and cc:
                        code_map[sc] = cc
            log.info(f"DART 기업코드 맵: {len(code_map)}개")
        except Exception as e:
            log.warning(f"corp_codes 로드 실패, 개별 조회로 대체: {e}")
            code_map = {}

        raw_targets = [
            {"code": (item.get("code") or "").split(".")[0], "name": item["name"]}
            for item in targets_raw
            if item.get("code")
        ]
        log.info(f"DB에서 {len(raw_targets)}개 종목 로드 — corp_code 일괄 조회 중...")

        targets = []
        for item in raw_targets:
            code = item["code"]
            corp_code = code_map.get(code)
            if not corp_code:
                # 개별 조회 fallback
                try:
                    info = dart.company(code)
                    if info is not None and not info.empty:
                        corp_code = str(info.iloc[0].get("corp_code", "")).strip()
                except Exception:
                    pass
                time.sleep(0.05)
            if corp_code:
                targets.append({
                    "corp_code":  corp_code,
                    "stock_code": code,
                    "corp_name":  item["name"],
                })
            else:
                log.debug(f"corp_code 없음: {item['name']} ({code})")

    log.info(f"수집 대상: {len(targets)}개 종목")

    # 기존 재무 데이터 캐시 로드 (성장률 계산용)
    log.info("기존 재무 데이터 캐시 로드 중...")
    fin_cache = build_fin_cache(sb)
    log.info(f"캐시 로드 완료: {sum(len(v) for v in fin_cache.values())}개 분기 데이터")

    # 순차 수집 (DART API 속도 제한 방지)
    collected = []
    skipped   = []  # DART 데이터 없음 (정상)
    failed    = []  # 오류 발생

    for i, t in enumerate(targets, 1):
        try:
            row = collect_one(dart, t["corp_code"], t["stock_code"], t["corp_name"], year, quarter)
            if row:
                # ── 누적→단독 변환 ──────────────────────────────
                # Q4: 연간 누적 → Q3 빼서 단독값 변환 (항상)
                # Q2/Q3: 대부분 단독값이나 누적 여부 자동 판별 후 변환
                fs_div   = row.get("fs_div", "CFS")
                prev_q_map = {"Q2": "Q1", "Q3": "Q2", "Q4": "Q3"}
                prev_q = prev_q_map.get(quarter)
                prev_row = None
                if prev_q:
                    if quarter == "Q4":
                        # Q4 변환: 반드시 DART Q3 누적값을 직접 조회
                        # (DB/캐시에는 Q3 단독값이 저장되어 있어 사용 불가)
                        prev_row = get_q3_cumulative(dart, t["corp_code"], year, fs_div)
                        if prev_row:
                            log.info(f"[Q3누적조회] {t['corp_name']} {year} Q4 변환용 Q3누적 조회 완료")
                        else:
                            # 폴백: DB에서 Q1+Q2+Q3 단독값 합산
                            log.warning(f"[Q3누적조회실패] {t['corp_name']} {year} — DB Q1+Q2+Q3 합산으로 폴백")
                            try:
                                _sb_fb = _get_sb()
                                _code_fb = t["stock_code"].split(".")[0]
                                _fb_res = _sb_fb.table("financials") \
                                    .select(",".join(["quarter","fs_div"] + FLOW_COLS)) \
                                    .eq("stock_code", _code_fb) \
                                    .eq("bsns_year", year) \
                                    .in_("quarter", ["Q1","Q2","Q3"]) \
                                    .eq("fs_div", fs_div).execute()
                                if _fb_res.data and len(_fb_res.data) == 3:
                                    prev_row = {}
                                    for col in FLOW_COLS:
                                        vals = [r.get(col) for r in _fb_res.data if r.get(col) is not None]
                                        if vals:
                                            prev_row[col] = sum(vals)
                                    log.info(f"[Q4폴백] {t['corp_name']} {year} Q1+Q2+Q3 합산 완료")
                                else:
                                    log.warning(f"[Q4폴백실패] {t['corp_name']} {year} Q1~Q3 데이터 부족 ({len(_fb_res.data)}개)")
                            except Exception as _fb_e:
                                log.warning(f"[Q4폴백오류] {t['corp_name']} {year}: {_fb_e}")
                    else:
                        # Q2/Q3: 캐시 또는 DB에서 이전 분기 단독값 조회
                        normalized_code = t["stock_code"].split(".")[0]
                        prev_cache_key = (year, prev_q, fs_div)
                        cached = fin_cache.get(normalized_code, {}).get(prev_cache_key)
                        if cached:
                            prev_row = cached
                            log.info(
                                f"[캐시HIT] {t['corp_name']} {year} {prev_q} → {quarter} 변환 "
                                f"(prev_revenue={cached.get('revenue')})"
                            )
                        else:
                            try:
                                res = sb.table("financials") \
                                    .select(",".join(["bsns_year","quarter","fs_div"] + FLOW_COLS)) \
                                    .eq("stock_code", normalized_code) \
                                    .eq("bsns_year", year) \
                                    .eq("quarter", prev_q) \
                                    .eq("fs_div", fs_div) \
                                    .limit(1).execute()
                                if res.data:
                                    prev_row = {col: res.data[0].get(col) for col in FLOW_COLS}
                                    log.info(f"[DB조회] {t['corp_name']} {year} {prev_q} → {quarter} 변환")
                                else:
                                    log.warning(
                                        f"[변환실패] {t['corp_name']} {year} {quarter} — "
                                        f"이전분기({prev_q}) DB에 없음 (stock_code={normalized_code}, fs_div={fs_div})"
                                    )
                            except Exception as pe:
                                log.warning(f"[DB조회실패] {t['corp_name']} {year} {prev_q}: {pe}")

                row = convert_to_pure_quarter(row, prev_row)

                # 캐시에 현재 순수값 추가 (이후 종목의 성장률 계산에 활용)
                normalized_code_cache = t["stock_code"].split(".")[0]
                if normalized_code_cache not in fin_cache:
                    fin_cache[normalized_code_cache] = {}
                cache_val = {col: row.get(col) for col in FLOW_COLS}
                fin_cache[normalized_code_cache][(year, quarter, fs_div)] = cache_val
                log.debug(
                    f"[캐시저장] {t['corp_name']} {year} {quarter} "
                    f"revenue={cache_val.get('revenue')} op={cache_val.get('operating_profit')}"
                )
                # ────────────────────────────────────────────────

                # 성장률 계산 (DB에 이전 분기 데이터 있을 때만)
                try:
                    growth = calculate_growth_rates(fin_cache, row)
                    row.update(growth)
                except Exception as ge:
                    log.debug(f"성장률 계산 실패 {t['corp_name']}: {ge}")
                collected.append(row)
                # DEBUG: Q4 저장값 확인
                if quarter == 'Q4' and row.get('corp_name') in ['아미코젠','피엔티','클래시스','한화시스템','농심','오스코텍']:
                    rev = row.get('revenue')
                    log.info(f"[DEBUG Q4저장] {row.get('corp_name')} {year} Q4 revenue={rev//int(1e8) if rev else None}억 cumulative={row.get('is_cumulative')}")
                log.info(f"[{i}/{len(targets)}] ✅ {t['corp_name']} "
                         f"({'누적유지' if row.get('is_cumulative') else '단독변환'})")
            else:
                skipped.append(t["corp_name"])
                log.debug(f"[{i}/{len(targets)}] SKIP {t['corp_name']} (데이터 없음)")
        except Exception as e:
            failed.append(t)
            log.error(f"[{i}/{len(targets)}] ❌ {t['corp_name']}: {e}")

        time.sleep(0.1)  # API 속도 제한 방지 (분당 최대 600회, DART 제한 1000회 이내)

    # 실패 종목 1회 재시도
    if failed:
        log.info(f"=== 실패 {len(failed)}개 재시도 ===")
        time.sleep(3)
        retry_failed = []
        for t in failed:
            try:
                row = collect_one(dart, t["corp_code"], t["stock_code"], t["corp_name"], year, quarter)
                if row:
                    fs_div = row.get("fs_div", "CFS")
                    prev_q_map = {"Q2": "Q1", "Q3": "Q2", "Q4": "Q3"}
                    prev_q = prev_q_map.get(quarter)
                    prev_row = None
                    if prev_q:
                        if quarter == "Q4":
                            prev_row = get_q3_cumulative(dart, t["corp_code"], year, fs_div)
                        else:
                            normalized_code = t["stock_code"].split(".")[0]
                            cached = fin_cache.get(normalized_code, {}).get((year, prev_q, fs_div))
                            if cached:
                                prev_row = cached
                            else:
                                try:
                                    res = sb.table("financials") \
                                        .select(",".join(["bsns_year","quarter","fs_div"] + FLOW_COLS)) \
                                        .eq("stock_code", normalized_code) \
                                        .eq("bsns_year", year) \
                                        .eq("quarter", prev_q) \
                                        .eq("fs_div", fs_div) \
                                        .limit(1).execute()
                                    if res.data:
                                        prev_row = {col: res.data[0].get(col) for col in FLOW_COLS}
                                except Exception as pe:
                                    log.warning(f"[재시도 DB조회실패] {t['corp_name']} {prev_q}: {pe}")
                    row = convert_to_pure_quarter(row, prev_row)
                    if t["stock_code"] not in fin_cache:
                        fin_cache[t["stock_code"]] = {}
                    fin_cache[t["stock_code"]][(year, quarter, fs_div)] = {
                        col: row.get(col) for col in FLOW_COLS
                    }
                    try:
                        growth = calculate_growth_rates(fin_cache, row)
                        row.update(growth)
                    except Exception:
                        pass
                    collected.append(row)
                    log.info(f"[재시도 ✅] {t['corp_name']}")
                else:
                    skipped.append(t["corp_name"])
            except Exception as e:
                retry_failed.append(t["corp_name"])
                log.error(f"[재시도 ❌] {t['corp_name']}: {e}")
            time.sleep(0.3)
        failed = retry_failed

    # Supabase upsert (50개씩 배치)
    batch_upsert(sb, "financials", collected,
                 "corp_code,bsns_year,quarter,fs_div",
                 chunk=50, progress_label="DB 저장")

    log.info(f"=== 완료: 수집 {len(collected)}개 / DART없음 {len(skipped)}개 / 실패 {len(failed)}개 ===")
    if failed:
        log.warning(f"최종 실패 종목: {', '.join(failed[:10])}" + ("..." if len(failed) > 10 else ""))
    if skipped:
        log.info(f"DART 데이터 없음 (정상): {', '.join(skipped[:10])}" + ("..." if len(skipped) > 10 else ""))

    return len(collected), len(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DART 재무 데이터 수집")
    parser.add_argument("--year",         type=str, help="사업연도 (예: 2024)")
    parser.add_argument("--quarter",      type=str, help="분기 (Q1/Q2/Q3/Q4)")
    parser.add_argument("--all-listed",   action="store_true", help="전체 상장사 수집")
    parser.add_argument("--workers",      type=int, default=3, help="병렬 워커 수")
    parser.add_argument("--corp-name",    type=str, nargs="+", help="특정 종목명 지정 (예: --corp-name 삼성전자 SK하이닉스)")
    parser.add_argument("--monitored-only", action="store_true",
                        help="모니터링 종목만 수집 (full+news, ~306개)")
    parser.add_argument("--non-monitored-only", action="store_true",
                        help="비모니터링 종목만 수집 (is_monitored=False, ~2,352개) — 주말 배치용")
    parser.add_argument("--all-quarters", action="store_true",
                        help="지정 연도 전분기 수집 (--year와 함께 사용)")
    parser.add_argument("--from-year",    type=str,
                        help="시작 연도부터 현재까지 전분기 수집 (예: --from-year 2023)")
    args = parser.parse_args()

    auto_y, auto_q = auto_detect_quarter()
    all_quarters   = ["Q1", "Q2", "Q3", "Q4"]

    if args.from_year:
        # 예: --from-year 2023 → 2023 Q1 ~ 현재 분기
        start_year = int(args.from_year)
        end_year   = int(auto_y)
        targets = []
        for y in range(start_year, end_year + 1):
            for q in all_quarters:
                if y == end_year and all_quarters.index(q) > all_quarters.index(auto_q):
                    break
                targets.append((str(y), q))
        log.info(f"=== 다중 분기 수집: {len(targets)}개 분기 ({args.from_year} Q1 ~ {auto_y} {auto_q}) ===")
        for y, q in targets:
            log.info(f"\n{'='*50}\n▶ {y} {q} 수집 시작\n{'='*50}")
            run(y, q, all_listed=args.all_listed, max_workers=args.workers, monitored_only=args.monitored_only, non_monitored_only=args.non_monitored_only, corp_names=args.corp_name)

    elif args.all_quarters:
        # 예: --year 2024 --all-quarters → 2024 Q1~Q4
        year = args.year or auto_y
        qs = all_quarters if year != auto_y else all_quarters[:all_quarters.index(auto_q) + 1]
        log.info(f"=== {year}년 전분기 수집: {qs} ===")
        for q in qs:
            log.info(f"\n{'='*50}\n▶ {year} {q} 수집 시작\n{'='*50}")
            run(year, q, all_listed=args.all_listed, max_workers=args.workers, monitored_only=args.monitored_only, non_monitored_only=args.non_monitored_only, corp_names=args.corp_name)

    else:
        year    = args.year    or auto_y
        quarter = args.quarter or auto_q
        if not args.year and not args.quarter:
            log.info(f"자동 감지: {year} {quarter}")
        run(year, quarter, all_listed=args.all_listed, max_workers=args.workers, monitored_only=args.monitored_only, non_monitored_only=args.non_monitored_only, corp_names=args.corp_name)
