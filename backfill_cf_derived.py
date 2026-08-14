"""
backfill_cf_derived.py
──────────────────────
현금흐름 파생 컬럼(capex_total·da·ebitda·fcf·fcf_direct·fcf_indirect) 재계산 백필.

배경(버그):
    calc_ratios 는 파생값을 '원본(누적) base'로 1차 계산하는데, 이후
    convert_to_pure_quarter 가 base(OCF·capex·capex_intangible 등)만 순분기로
    변환하고 파생 컬럼은 그대로 두었다. 그 결과 Q2/Q3/Q4 행의 파생 컬럼이
    '누적값'으로 저장돼, 프론트가 4분기를 합산하는 연간 뷰에서 FCF·CapEx가
    최대 ~2배 과대집계됐다. (예: SK하이닉스 2024 FCF 30.4조 표시 → 실제 15.3조)

수정:
    collect_financials.convert_to_pure_quarter 가 순분기 변환 후 파생값을 재계산하도록
    고쳤다(신규 수집분 정상). 이 스크립트는 '이미 저장된' 과거 행을 교정한다.

방법:
    base 컬럼은 이미 순분기로 올바르게 저장돼 있으므로 DART 재호출 없이
    calc_cashflow_derived(row) 로 파생값만 다시 계산해 값이 바뀐 행만 재저장한다.
    ⚠️ PostgREST 부분컬럼 upsert 은 NOT NULL 을 충돌판정보다 먼저 검사해 실패하므로,
       (collect_utils.batch_update_existing 주석 참조) 행 '전체'를 다시 upsert 한다
       (파생 6개만 교정, 나머지 컬럼은 기존값 그대로).

실행:
    python backfill_cf_derived.py                 # 드라이런(기본): 변경 예정 건수·샘플만 출력
    python backfill_cf_derived.py --apply         # 실제 저장 (service key 필요)
    python backfill_cf_derived.py --stock 000660  # 특정 종목만 (검증용)
    python backfill_cf_derived.py --apply --stock 000660
"""

import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
log = get_logger(__name__)

from collect_financials import calc_cashflow_derived, DERIVED_CF_COLS
from collect_utils import fetch_all_pages, batch_upsert

CONFLICT = "corp_code,bsns_year,quarter,fs_div"
# upsert 재저장 시 제외할 컬럼(PK/타임스탬프는 DB가 관리 — 건드리지 않음)
_SKIP_COLS = ("id", "collected_at", "updated_at")


def _make_client(need_write: bool):
    """service key 우선(쓰기), 없으면 anon key 로 폴백(드라이런 읽기 전용)."""
    url = os.getenv("SB_URL", "")
    svc = os.getenv("SB_SERVICE_KEY", "")
    anon = os.getenv("SB_KEY", "")
    if need_write and not svc:
        log.error("--apply 는 SB_SERVICE_KEY 가 필요합니다 (anon 키는 RLS로 쓰기 불가).")
        sys.exit(1)
    key = svc or anon
    if not (url and key):
        # 표준 경로 폴백
        from db_client import get_supabase_client
        return get_supabase_client()
    from supabase import create_client
    return create_client(url, key)


def _changed_derived(row: dict):
    """행의 base 로 파생값 재계산 → 기존과 다르면 {col: newval} 반환, 같으면 None."""
    recomputed = calc_cashflow_derived(row)
    diff = {}
    for c in DERIVED_CF_COLS:
        new = recomputed.get(c)          # 계산 불가 시 None
        old = row.get(c)
        if new != old:
            diff[c] = new
    return diff or None


def _fmt(v):
    if v is None:
        return "None"
    return f"{round(v / 1e8):,}억"


def run(apply: bool = False, stock: str = None, sample: int = 12):
    sb = _make_client(need_write=apply)

    q = sb.table("financials").select("*")
    if stock:
        q = q.eq("stock_code", stock)
    rows = fetch_all_pages(q)
    log.info(f"스캔 대상: {len(rows):,}행" + (f" (stock={stock})" if stock else ""))

    payloads = []
    by_quarter = {}
    samples = []
    for row in rows:
        diff = _changed_derived(row)
        if not diff:
            continue
        by_quarter[row.get("quarter")] = by_quarter.get(row.get("quarter"), 0) + 1
        if len(samples) < sample:
            samples.append((row, diff))
        payload = {k: v for k, v in row.items() if k not in _SKIP_COLS}
        payload.update(diff)
        payloads.append(payload)

    # ── 리포트 ──
    print("\n" + "=" * 66)
    print("  현금흐름 파생 컬럼 재계산 백필" + ("  [APPLY]" if apply else "  [DRY-RUN]"))
    print("=" * 66)
    print(f"  스캔 행       : {len(rows):>8,}")
    print(f"  변경 예정 행  : {len(payloads):>8,}")
    if by_quarter:
        bq = "  ".join(f"{k}:{v:,}" for k, v in sorted(by_quarter.items()))
        print(f"  분기별        : {bq}")
    print("=" * 66)

    if samples:
        print("\n  샘플 (기존 → 재계산):")
        for row, diff in samples:
            tag = f"{row.get('corp_name','?')} {row.get('bsns_year')} {row.get('quarter')} {row.get('fs_div')}"
            chips = ", ".join(f"{c} {_fmt(row.get(c))}→{_fmt(diff[c])}" for c in diff)
            print(f"   - {tag:<26} {chips}")

    if not payloads:
        print("\n  변경할 행 없음 — 이미 모두 정합.")
        return

    if not apply:
        print(f"\n  드라이런입니다. 실제 저장하려면 --apply 를 붙이세요.")
        return

    print(f"\n  {len(payloads):,}행 재저장(upsert) 시작...")
    n = batch_upsert(sb, "financials", payloads, CONFLICT,
                     chunk=100, progress_label="파생 백필", sleep=0.05,
                     raise_on_error=True)
    print(f"  완료: {n:,}행 저장.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제 저장 (기본은 드라이런)")
    ap.add_argument("--stock", type=str, default=None, help="특정 stock_code 만")
    ap.add_argument("--sample", type=int, default=12, help="샘플 출력 개수")
    args = ap.parse_args()
    run(apply=args.apply, stock=args.stock, sample=args.sample)


if __name__ == "__main__":
    main()
