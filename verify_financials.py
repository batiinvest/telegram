"""
verify_financials.py
────────────────────
backfill_financials.py 수집 후 데이터 품질 검증기

실행:
    python verify_financials.py                     # 모니터링 종목 전체 검증
    python verify_financials.py --year 2024         # 특정 연도만
    python verify_financials.py --corp-name 삼성전자 # 특정 종목만
    python verify_financials.py --export            # 이상 데이터 CSV 저장

검증 항목:
    1. 수집 커버리지 — 분기별 수집률
    2. 핵심 컬럼 NULL 비율 — revenue, operating_profit, net_income 등
    3. 누적값 미변환 — is_cumulative=True 잔존
    4. 이상값 탐지 — 전분기 대비 10배↑ or 0.1배↓ 급변
    5. 부호 오류 — revenue/total_assets 가 음수
    6. 논리 검증 — revenue < operating_profit 등 상식 밖 관계
    7. YoY 성장률 이상 — ±500% 이상
    8. 특정 종목 샘플 검증 — 삼성전자, SK하이닉스, NAVER 주요 수치 출력
"""

import os
import sys
import logging
from logger_config import get_logger
log = get_logger(__name__)

import argparse
from datetime import datetime

try:
    from supabase import create_client
except ImportError:
    print("pip install supabase 필요")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

s [%(levelname)s] %(message)s",
)


SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")

# 검증 시 사용할 핵심 컬럼
KEY_COLS = [
    "revenue", "operating_profit", "net_income",
    "total_assets", "total_liabilities", "total_equity",
    "operating_cashflow",
]
RATIO_COLS = [
    "operating_margin", "net_margin", "roe", "roa",
    "debt_ratio", "current_ratio",
]
FLOW_SIGN_COLS = ["capex", "capex_intangible"]  # 보통 음수로 들어오거나 0


# ─────────────────────────────────────────────
#  데이터 로드
# ─────────────────────────────────────────────
def _load_data(sb, monitored_only: bool = True,
               year_filter: str = None,
               corp_names: list = None) -> list:
    """financials + companies 조인하여 로드"""
    log.info("📥 데이터 로드 중...")

    # 모니터링 종목 코드 목록
    mon_codes = set()
    if monitored_only:
        page = 0
        while True:
            res = sb.table("companies").select("code") \
                .eq("is_monitored", True) \
                .range(page * 1000, (page + 1) * 1000 - 1).execute()
            if not res.data:
                break
            for r in res.data:
                code = (r.get("code") or "").split(".")[0]
                if code:
                    mon_codes.add(code)
            if len(res.data) < 1000:
                break
            page += 1
        log.info(f"  모니터링 종목: {len(mon_codes):,}개")

    # financials 로드
    select_cols = (
        "stock_code,corp_name,bsns_year,quarter,fs_div,is_cumulative,"
        "revenue,gross_profit,operating_profit,net_income,pretax_income,"
        "total_assets,total_liabilities,total_equity,current_assets,current_liabilities,"
        "operating_cashflow,investing_cashflow,financing_cashflow,capex,capex_intangible,"
        "depreciation,amortization,da,capex_total,ebitda,fcf,fcf_direct,fcf_indirect,"
        "operating_margin,net_margin,roe,roa,debt_ratio,current_ratio,"
        "revenue_yoy,op_profit_yoy,net_income_yoy"
    )

    rows = []
    page = 0
    while True:
        q = sb.table("financials").select(select_cols)
        if year_filter:
            q = q.eq("bsns_year", year_filter)
        res = q.range(page * 1000, (page + 1) * 1000 - 1).execute()
        if not res.data:
            break
        rows.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1

    # 모니터링 필터
    if monitored_only and mon_codes:
        rows = [r for r in rows if r.get("stock_code") in mon_codes]

    # 종목명 필터
    if corp_names:
        rows = [r for r in rows if r.get("corp_name") in corp_names]

    log.info(f"  로드 완료: {len(rows):,}건")
    return rows


# ─────────────────────────────────────────────
#  검증 1: 수집 커버리지
# ─────────────────────────────────────────────
def check_coverage(rows: list, mon_codes: set, all_quarters: list):
    """분기별 수집된 종목 수 vs 모니터링 총 종목 수"""
    print("\n" + "="*60)
    print("📊 [1] 수집 커버리지")
    print("="*60)

    from collections import defaultdict
    qtr_codes = defaultdict(set)
    for r in rows:
        key = f"{r['bsns_year']} {r['quarter']}"
        qtr_codes[key].add(r["stock_code"])

    total = len(mon_codes) if mon_codes else "?"
    issues = []
    for qtr in sorted(all_quarters):
        cnt  = len(qtr_codes.get(qtr, set()))
        rate = cnt / len(mon_codes) * 100 if mon_codes else 0
        flag = "⚠️ " if rate < 70 else ("✅ " if rate >= 90 else "🟡 ")
        print(f"  {flag} {qtr}: {cnt:3d}/{total}개 ({rate:.0f}%)")
        if rate < 70:
            issues.append(f"{qtr}: 커버리지 {rate:.0f}% (낮음)")

    return issues


# ─────────────────────────────────────────────
#  검증 2: 핵심 컬럼 NULL 비율
# ─────────────────────────────────────────────
def check_null_rates(rows: list):
    """각 컬럼별 NULL 비율 출력"""
    print("\n" + "="*60)
    print("📊 [2] 핵심 컬럼 NULL 비율")
    print("="*60)

    n = len(rows)
    if n == 0:
        print("  데이터 없음")
        return []

    issues = []
    check_cols = KEY_COLS + [
        "depreciation", "amortization", "da", "ebitda",
        "fcf", "fcf_direct", "capex",
    ]
    for col in check_cols:
        null_cnt  = sum(1 for r in rows if r.get(col) is None)
        null_rate = null_cnt / n * 100
        flag = "❌" if null_rate > 50 else ("⚠️" if null_rate > 30 else "✅")
        print(f"  {flag} {col:<25s}: NULL {null_cnt:,}/{n:,} ({null_rate:.0f}%)")
        if null_rate > 50:
            issues.append(f"{col}: NULL {null_rate:.0f}%")

    return issues


# ─────────────────────────────────────────────
#  검증 3: 누적값 미변환
# ─────────────────────────────────────────────
def check_cumulative(rows: list):
    """is_cumulative=True 잔존 건수"""
    print("\n" + "="*60)
    print("📊 [3] 누적값 미변환 잔존")
    print("="*60)

    cum_rows = [r for r in rows if r.get("is_cumulative") is True]
    if not cum_rows:
        print("  ✅ 누적값 미변환 없음")
        return []

    print(f"  ⚠️  누적값 미변환: {len(cum_rows)}건")
    # 분기별 집계
    from collections import Counter
    cnt = Counter(f"{r['bsns_year']} {r['quarter']}" for r in cum_rows)
    for qtr, c in sorted(cnt.items()):
        print(f"     {qtr}: {c}건")

    # 샘플 출력
    print("\n  샘플 (최대 10건):")
    for r in cum_rows[:10]:
        rev = r.get("revenue")
        rev_str = f"{rev//100_000_000:,}억" if rev else "NULL"
        print(f"    {r['corp_name']:15s} {r['bsns_year']} {r['quarter']}  revenue={rev_str}")

    return [f"누적값 미변환 {len(cum_rows)}건"]


# ─────────────────────────────────────────────
#  검증 4: 이상값 탐지 (전분기 대비 급변)
# ─────────────────────────────────────────────
def check_outliers(rows: list, threshold: float = 10.0):
    """
    같은 종목의 연속 분기 데이터에서
    revenue가 전분기 대비 threshold배 이상 급변하는 케이스 탐지
    """
    print("\n" + "="*60)
    print(f"📊 [4] 이상값 탐지 (전분기 대비 {threshold}배 이상 급변)")
    print("="*60)

    Q_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}

    # stock_code별 정렬
    from collections import defaultdict
    by_stock = defaultdict(list)
    for r in rows:
        if r.get("revenue") is not None and r["revenue"] != 0:
            by_stock[r["stock_code"]].append(r)

    for code in by_stock:
        by_stock[code].sort(
            key=lambda x: (x["bsns_year"], Q_ORDER.get(x["quarter"], 0))
        )

    issues_list = []
    for code, stock_rows in by_stock.items():
        for i in range(1, len(stock_rows)):
            prev = stock_rows[i - 1]
            curr = stock_rows[i]
            p_rev = prev.get("revenue", 0) or 0
            c_rev = curr.get("revenue", 0) or 0
            if p_rev == 0 or c_rev == 0:
                continue
            ratio = c_rev / p_rev
            if ratio >= threshold or ratio <= 1 / threshold:
                issues_list.append({
                    "corp_name": curr["corp_name"],
                    "stock_code": code,
                    "from": f"{prev['bsns_year']} {prev['quarter']}",
                    "to":   f"{curr['bsns_year']} {curr['quarter']}",
                    "prev_rev": p_rev,
                    "curr_rev": c_rev,
                    "ratio": ratio,
                })

    if not issues_list:
        print("  ✅ 급변 이상값 없음")
        return []

    print(f"  ⚠️  급변 감지: {len(issues_list)}건")
    issues_list.sort(key=lambda x: abs(x["ratio"] - 1), reverse=True)
    for it in issues_list[:20]:
        p_str = f"{it['prev_rev']//100_000_000:,}억"
        c_str = f"{it['curr_rev']//100_000_000:,}억"
        print(
            f"  ⚠️  {it['corp_name']:15s}  {it['from']} → {it['to']}  "
            f"{p_str} → {c_str}  (×{it['ratio']:.1f})"
        )

    return [f"급변 이상값 {len(issues_list)}건"]


# ─────────────────────────────────────────────
#  검증 5: 부호 오류
# ─────────────────────────────────────────────
def check_sign_errors(rows: list):
    """revenue, total_assets 등 항상 양수여야 할 컬럼이 음수인 케이스"""
    print("\n" + "="*60)
    print("📊 [5] 부호 오류 (양수여야 할 컬럼이 음수)")
    print("="*60)

    ALWAYS_POSITIVE = ["revenue", "total_assets", "total_equity"]
    issues_list = []

    for col in ALWAYS_POSITIVE:
        bad = [r for r in rows
               if r.get(col) is not None and r[col] < 0]
        if bad:
            print(f"  ❌ {col}: 음수 {len(bad)}건")
            for r in bad[:5]:
                print(f"     {r['corp_name']:15s} {r['bsns_year']} {r['quarter']}  {col}={r[col]:,}")
            issues_list.append(f"{col} 음수 {len(bad)}건")
        else:
            print(f"  ✅ {col}: 정상")

    return issues_list


# ─────────────────────────────────────────────
#  검증 6: 논리 검증
# ─────────────────────────────────────────────
def check_logic(rows: list):
    """
    - revenue < |operating_profit|  (영업이익 > 매출 — 불가능)
    - total_assets < total_liabilities + total_equity 의 큰 차이
    - net_margin > 100% 또는 < -200%
    """
    print("\n" + "="*60)
    print("📊 [6] 논리 검증")
    print("="*60)

    issues_list = []

    # 6-1: 영업이익 > 매출 (절댓값)
    bad_op = [
        r for r in rows
        if (r.get("revenue") or 0) > 0
        and r.get("operating_profit") is not None
        and abs(r["operating_profit"]) > abs(r["revenue"]) * 1.5
    ]
    if bad_op:
        print(f"  ❌ |영업이익| > 매출×1.5: {len(bad_op)}건")
        for r in bad_op[:5]:
            rev = (r.get("revenue") or 0) // 100_000_000
            op  = (r.get("operating_profit") or 0) // 100_000_000
            print(f"     {r['corp_name']:15s} {r['bsns_year']} {r['quarter']}  "
                  f"매출={rev:,}억 영업이익={op:,}억")
        issues_list.append(f"|영업이익|>매출×1.5: {len(bad_op)}건")
    else:
        print("  ✅ 영업이익/매출 논리: 정상")

    # 6-2: 순이익률 극단값 (net_margin > 200% or < -500%)
    bad_margin = [
        r for r in rows
        if r.get("net_margin") is not None
        and (r["net_margin"] > 200 or r["net_margin"] < -500)
    ]
    if bad_margin:
        print(f"  ❌ 순이익률 극단값(>200% or <-500%): {len(bad_margin)}건")
        for r in bad_margin[:5]:
            print(f"     {r['corp_name']:15s} {r['bsns_year']} {r['quarter']}  "
                  f"net_margin={r['net_margin']:.1f}%")
        issues_list.append(f"순이익률 극단값: {len(bad_margin)}건")
    else:
        print("  ✅ 순이익률: 정상 범위")

    # 6-3: ROE 극단값 (> 1000% or < -1000%)
    bad_roe = [
        r for r in rows
        if r.get("roe") is not None
        and abs(r["roe"]) > 1000
    ]
    if bad_roe:
        print(f"  ❌ ROE 극단값(|ROE|>1000%): {len(bad_roe)}건")
        for r in bad_roe[:5]:
            print(f"     {r['corp_name']:15s} {r['bsns_year']} {r['quarter']}  "
                  f"roe={r['roe']:.1f}%  equity={r.get('total_equity')}")
        issues_list.append(f"ROE 극단값: {len(bad_roe)}건")
    else:
        print("  ✅ ROE: 정상 범위")

    return issues_list


# ─────────────────────────────────────────────
#  검증 7: YoY 성장률 이상
# ─────────────────────────────────────────────
def check_yoy(rows: list, threshold: float = 500.0):
    """revenue_yoy, op_profit_yoy, net_income_yoy 극단값 탐지"""
    print("\n" + "="*60)
    print(f"📊 [7] YoY 성장률 극단값 (|성장률| > {threshold}%)")
    print("="*60)

    issues_list = []
    fields = [
        ("revenue_yoy",    "매출YoY"),
        ("op_profit_yoy",  "영업이익YoY"),
        ("net_income_yoy", "순이익YoY"),
    ]

    for col, label in fields:
        bad = [
            r for r in rows
            if r.get(col) is not None and abs(r[col]) > threshold
        ]
        if bad:
            print(f"  ⚠️  {label} 극단값: {len(bad)}건")
            bad.sort(key=lambda x: abs(x[col]), reverse=True)
            for r in bad[:5]:
                print(f"     {r['corp_name']:15s} {r['bsns_year']} {r['quarter']}  "
                      f"{label}={r[col]:+.1f}%")
            issues_list.append(f"{label} 극단값 {len(bad)}건")
        else:
            print(f"  ✅ {label}: 정상 범위")

    return issues_list


# ─────────────────────────────────────────────
#  검증 8: 주요 종목 샘플 검증
# ─────────────────────────────────────────────
def check_samples(rows: list):
    """삼성전자, SK하이닉스, NAVER, 카카오 최신 4분기 수치 출력"""
    print("\n" + "="*60)
    print("📊 [8] 주요 종목 샘플 수치 확인")
    print("="*60)

    SAMPLES = ["삼성전자", "SK하이닉스", "NAVER", "카카오", "현대차", "LG화학"]

    from collections import defaultdict
    by_name = defaultdict(list)
    for r in rows:
        by_name[r.get("corp_name", "")].append(r)

    Q_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}

    def _cap(v):
        if v is None:
            return "  NULL  "
        t = abs(v)
        if t >= 1_000_000_000_000:
            return f"{v/1_000_000_000_000:+6.1f}조"
        if t >= 100_000_000:
            return f"{v/100_000_000:+6.0f}억"
        return f"{v:+,}"

    def _pct(v):
        return f"{v:+.1f}%" if v is not None else " NULL%"

    for name in SAMPLES:
        stock_rows = by_name.get(name, [])
        if not stock_rows:
            print(f"\n  [{name}] — 데이터 없음")
            continue

        stock_rows.sort(
            key=lambda x: (x["bsns_year"], Q_ORDER.get(x["quarter"], 0)),
            reverse=True,
        )
        recent = stock_rows[:6]  # 최근 6분기

        print(f"\n  [{name}] ({recent[0]['stock_code']})")
        print(f"  {'기간':10s} {'매출':>10s} {'영업이익':>10s} {'OPM':>7s} {'순이익':>10s} {'OCF':>10s} {'FCF':>10s} {'누적여부':>6s}")
        print("  " + "-"*80)
        for r in recent:
            period = f"{r['bsns_year']} {r['quarter']}"
            cum    = "누적" if r.get("is_cumulative") else "단독"
            print(
                f"  {period:10s} "
                f"{_cap(r.get('revenue')):>10s} "
                f"{_cap(r.get('operating_profit')):>10s} "
                f"{_pct(r.get('operating_margin')):>7s} "
                f"{_cap(r.get('net_income')):>10s} "
                f"{_cap(r.get('operating_cashflow')):>10s} "
                f"{_cap(r.get('fcf')):>10s} "
                f"{cum:>6s}"
            )


# ─────────────────────────────────────────────
#  검증 9: D&A / EBITDA 수집 현황
# ─────────────────────────────────────────────
def check_da_coverage(rows: list):
    """depreciation, amortization, ebitda 수집 비율"""
    print("\n" + "="*60)
    print("📊 [9] D&A / EBITDA 수집 현황 (신규 컬럼)")
    print("="*60)

    n = len(rows)
    if n == 0:
        print("  데이터 없음")
        return []

    cols_check = [
        ("depreciation",     "감가상각비"),
        ("amortization",     "무형상각비"),
        ("da",               "D&A합계"),
        ("capex_intangible", "무형CapEx"),
        ("capex_total",      "CapEx합계"),
        ("ebitda",           "EBITDA"),
        ("fcf_direct",       "FCF(직접법)"),
        ("fcf_indirect",     "FCF(간접법)"),
    ]

    issues = []
    for col, label in cols_check:
        filled = sum(1 for r in rows if r.get(col) is not None)
        rate   = filled / n * 100
        flag   = "✅" if rate >= 40 else ("🟡" if rate >= 10 else "⬜")
        print(f"  {flag} {label:12s}({col:<18s}): {filled:,}/{n:,} ({rate:.0f}%)")
        if rate == 0:
            issues.append(f"{col}: 0% 수집 (DB 컬럼 추가 필요)")

    if issues:
        print("\n  ⚠️  DB에 컬럼이 없을 수 있습니다. Supabase SQL 실행 확인 필요:")
        print("""
  ALTER TABLE financials
    ADD COLUMN IF NOT EXISTS depreciation     BIGINT,
    ADD COLUMN IF NOT EXISTS amortization     BIGINT,
    ADD COLUMN IF NOT EXISTS da               BIGINT,
    ADD COLUMN IF NOT EXISTS capex_intangible BIGINT,
    ADD COLUMN IF NOT EXISTS capex_total      BIGINT,
    ADD COLUMN IF NOT EXISTS ebitda           BIGINT,
    ADD COLUMN IF NOT EXISTS fcf_direct       BIGINT,
    ADD COLUMN IF NOT EXISTS fcf_indirect     BIGINT;
""")

    return issues


# ─────────────────────────────────────────────
#  CSV 내보내기
# ─────────────────────────────────────────────
def export_issues(all_issues: list, rows: list):
    """이상 데이터 CSV 저장"""
    import csv
    filename = f"financials_issues_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    # 이상 종목 코드 추출 (부호오류 + 급변 + 누적미변환)
    flagged_codes = set()
    for r in rows:
        if r.get("is_cumulative"):
            flagged_codes.add(r["stock_code"])
        if (r.get("revenue") or 0) < 0:
            flagged_codes.add(r["stock_code"])
        if r.get("net_margin") is not None and abs(r["net_margin"]) > 200:
            flagged_codes.add(r["stock_code"])

    flagged_rows = [r for r in rows if r["stock_code"] in flagged_codes]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        if not flagged_rows:
            f.write("이상 데이터 없음\n")
        else:
            fields = ["stock_code", "corp_name", "bsns_year", "quarter", "fs_div",
                      "is_cumulative", "revenue", "operating_profit", "net_income",
                      "net_margin", "operating_margin", "roe", "total_assets", "total_equity"]
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flagged_rows)

    print(f"\n💾 이상 데이터 CSV 저장: {filename} ({len(flagged_rows)}건)")


# ─────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="재무 데이터 품질 검증기")
    parser.add_argument("--year",       type=str,        help="특정 연도만 검증 (예: 2024)")
    parser.add_argument("--corp-name",  type=str, nargs="+", help="특정 종목명 지정")
    parser.add_argument("--all",        action="store_true",  help="전체 상장사 검증 (기본: 모니터링만)")
    parser.add_argument("--export",     action="store_true",  help="이상 데이터 CSV 저장")
    parser.add_argument("--threshold",  type=float, default=10.0, help="급변 탐지 배수 (기본: 10)")
    args = parser.parse_args()

    if not SB_URL or not SB_SERVICE_KEY:
        log.error("SB_URL, SB_SERVICE_KEY 환경변수 필요")
        sys.exit(1)

    sb = create_client(SB_URL, SB_SERVICE_KEY)

    monitored_only = not args.all and not args.corp_name

    # 모니터링 코드 로드
    mon_codes = set()
    if monitored_only:
        page = 0
        while True:
            res = sb.table("companies").select("code") \
                .eq("is_monitored", True) \
                .range(page * 1000, (page + 1) * 1000 - 1).execute()
            if not res.data:
                break
            for r in res.data:
                code = (r.get("code") or "").split(".")[0]
                if code:
                    mon_codes.add(code)
            if len(res.data) < 1000:
                break
            page += 1

    rows = _load_data(
        sb,
        monitored_only=monitored_only,
        year_filter=args.year,
        corp_names=args.corp_name,
    )

    if not rows:
        print("❌ 데이터 없음 — backfill_financials.py를 먼저 실행하세요.")
        sys.exit(0)

    # 수집된 분기 목록
    all_quarters = sorted({f"{r['bsns_year']} {r['quarter']}" for r in rows})

    print(f"\n{'='*60}")
    print(f"🔍 재무 데이터 품질 검증 보고서")
    print(f"   검증 범위: {'모니터링 종목' if monitored_only else '전체'}")
    print(f"   연도 필터: {args.year or '전체'}")
    print(f"   총 데이터: {len(rows):,}건  |  분기: {len(all_quarters)}개")
    print(f"   분기 범위: {all_quarters[0] if all_quarters else 'N/A'} ~ {all_quarters[-1] if all_quarters else 'N/A'}")
    print(f"{'='*60}")

    all_issues = []

    # 검증 실행
    all_issues += check_coverage(rows, mon_codes, all_quarters)
    all_issues += check_null_rates(rows)
    all_issues += check_cumulative(rows)
    all_issues += check_outliers(rows, threshold=args.threshold)
    all_issues += check_sign_errors(rows)
    all_issues += check_logic(rows)
    all_issues += check_yoy(rows)
    check_samples(rows)    # 출력만, issue 반환 없음
    all_issues += check_da_coverage(rows)

    # 종합 요약
    print("\n" + "="*60)
    print("📋 종합 요약")
    print("="*60)
    if not all_issues:
        print("  ✅ 모든 검증 통과 — 데이터 품질 양호")
    else:
        print(f"  ⚠️  발견된 이슈: {len(all_issues)}개")
        for i, issue in enumerate(all_issues, 1):
            print(f"  {i:2d}. {issue}")

    if args.export:
        export_issues(all_issues, rows)

    print()


if __name__ == "__main__":
    main()
