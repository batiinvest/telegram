"""
backfill_financials.py
──────────────────────
재무 데이터 과거분 일괄 백필 수집기
- 이미 DB에 있는 (stock_code, year, quarter) 는 스킵
- 모니터링 종목(is_monitored=True) 우선, 이후 비모니터링 순서

실행 예시:
    # 모니터링 종목만 (기본)
    python backfill_financials.py --from-year 2023

    # 전체 상장사 (시간 오래 걸림)
    python backfill_financials.py --from-year 2023 --all

    # 비모니터링만 (모니터링 완료 후 이어서)
    python backfill_financials.py --from-year 2023 --non-monitored-only

    # 특정 종목 지정
    python backfill_financials.py --from-year 2022 --corp-name 삼성전자 SK하이닉스
"""

import os
import sys
import time
from logger_config import get_logger
log = get_logger(__name__)

import argparse

try:
    from db_client import get_supabase_client as _get_sb
except ImportError:
    from supabase import create_client as _cs
    def _get_sb(): return _cs(os.getenv("SB_URL",""), os.getenv("SB_SERVICE_KEY",""))

try:
    import OpenDartReader
except ImportError:
    print("pip install opendartreader 필요")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

# collect_financials 에서 핵심 함수 재사용
from collect_financials import (
    collect_one,
    convert_to_pure_quarter,
    calculate_growth_rates,
    get_q3_cumulative,
    build_fin_cache,
    FLOW_COLS,
    auto_detect_quarter,
)



DART_API_KEY   = os.getenv("DART_API_KEY", "")
SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")

# ──────────────────────────────────────────────────────────────
#  수집 대상 분기 목록 생성 (from_year ~ 최신 분기)
# ──────────────────────────────────────────────────────────────
def _build_quarter_list(from_year: int) -> list:
    """from_year Q1 부터 현재까지 수집 가능한 분기 목록"""
    auto_y, auto_q = auto_detect_quarter()
    all_q = ["Q1", "Q2", "Q3", "Q4"]
    targets = []
    for y in range(from_year, int(auto_y) + 1):
        for q in all_q:
            if y == int(auto_y) and all_q.index(q) > all_q.index(auto_q):
                break
            targets.append((str(y), q))
    return targets


# ──────────────────────────────────────────────────────────────
#  DB에서 이미 수집된 (stock_code, year, quarter) 세트 로드
# ──────────────────────────────────────────────────────────────
def _load_existing(sb) -> set:
    """
    financials 테이블에서 이미 수집된 키 세트 반환.
    {(stock_code, bsns_year, quarter), ...}
    """
    log.info("📂 기존 수집 데이터 로드 중...")
    existing = set()
    page = 0
    while True:
        res = sb.table("financials") \
            .select("stock_code,bsns_year,quarter") \
            .range(page * 1000, (page + 1) * 1000 - 1) \
            .execute()
        if not res.data:
            break
        for r in res.data:
            existing.add((r["stock_code"], r["bsns_year"], r["quarter"]))
        if len(res.data) < 1000:
            break
        page += 1
    log.info(f"📂 기존 수집: {len(existing):,}건")
    return existing


# ──────────────────────────────────────────────────────────────
#  회사 목록 로드 (monitored 우선 → 비모니터링)
# ──────────────────────────────────────────────────────────────
def _load_companies(sb, monitored_only: bool = False,
                    non_monitored_only: bool = False,
                    corp_names: list = None) -> list:
    """
    companies 테이블에서 수집 대상 로드.
    반환: [{"stock_code": ..., "corp_name": ..., "corp_code": ...}, ...]
    """
    dart = OpenDartReader(DART_API_KEY)

    # DART 전체 corp_code 맵 (stock_code → corp_code)
    log.info("📡 DART 기업코드 맵 로딩...")
    try:
        corp_df  = dart.corp_codes
        code_map = {}
        if corp_df is not None and not corp_df.empty:
            for _, row in corp_df.iterrows():
                sc = str(row.get("stock_code", "")).strip()
                cc = str(row.get("corp_code",  "")).strip()
                if sc and cc:
                    code_map[sc] = cc
        log.info(f"📡 DART 기업코드 맵: {len(code_map):,}개")
    except Exception as e:
        log.warning(f"DART 기업코드 맵 로드 실패: {e}")
        code_map = {}

    # companies 테이블 조회
    q = sb.table("companies").select("name,code,corp_code")
    if monitored_only:
        q = q.eq("is_monitored", True)
        log.info("🎯 모니터링 종목만 (is_monitored=True)")
    elif non_monitored_only:
        q = q.eq("active", True).eq("is_monitored", False)
        log.info("📋 비모니터링 종목만 (is_monitored=False, active=True)")
    else:
        q = q.eq("active", True)
        log.info("📋 전체 상장사 (active=True)")

    raw = []
    page = 0
    while True:
        res = q.range(page * 1000, (page + 1) * 1000 - 1).execute()
        if not res.data:
            break
        raw.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1

    if corp_names:
        raw = [r for r in raw if r.get("name") in corp_names]
        log.info(f"🔍 종목명 필터: {corp_names} → {len(raw)}개")

    raw = [r for r in raw if '스팩' not in (r.get('name') or '')]
    # corp_code 매핑
    targets = []
    for item in raw:
        code      = (item.get("code") or "").split(".")[0]
        corp_code = item.get("corp_code") or code_map.get(code)
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
                "stock_code": code,
                "corp_name":  item.get("name", code),
                "corp_code":  corp_code,
            })

    log.info(f"✅ 수집 대상: {len(targets):,}개 종목")
    return targets


# ──────────────────────────────────────────────────────────────
#  단일 (종목, 분기) 수집 + DB upsert
# ──────────────────────────────────────────────────────────────
def _collect_and_save(dart, sb, fin_cache: dict, t: dict,
                      year: str, quarter: str) -> bool:
    """
    Returns True if successfully collected and saved.
    """
    corp_code  = t["corp_code"]
    stock_code = t["stock_code"]
    corp_name  = t["corp_name"]

    row = collect_one(dart, corp_code, stock_code, corp_name, year, quarter)
    if not row:
        return False

    fs_div = row.get("fs_div", "CFS")
    prev_q_map = {"Q2": "Q1", "Q3": "Q2", "Q4": "Q3"}
    prev_q     = prev_q_map.get(quarter)
    prev_row   = None

    if prev_q:
        if quarter == "Q4":
            prev_row = get_q3_cumulative(dart, corp_code, year, fs_div)
            if not prev_row:
                # DB 폴백: Q1+Q2+Q3 합산
                try:
                    fb = sb.table("financials") \
                        .select(",".join(["quarter", "fs_div"] + FLOW_COLS)) \
                        .eq("stock_code", stock_code) \
                        .eq("bsns_year", year) \
                        .in_("quarter", ["Q1", "Q2", "Q3"]) \
                        .eq("fs_div", fs_div).execute()
                    if fb.data and len(fb.data) == 3:
                        prev_row = {}
                        for col in FLOW_COLS:
                            vals = [r.get(col) for r in fb.data if r.get(col) is not None]
                            if vals:
                                prev_row[col] = sum(vals)
                except Exception as e:
                    log.warning(f"[Q4폴백오류] {corp_name} {year}: {e}")
        else:
            cached = fin_cache.get(stock_code, {}).get((year, prev_q, fs_div))
            if cached:
                prev_row = cached
            else:
                try:
                    res = sb.table("financials") \
                        .select(",".join(["bsns_year", "quarter", "fs_div"] + FLOW_COLS)) \
                        .eq("stock_code", stock_code) \
                        .eq("bsns_year", year) \
                        .eq("quarter", prev_q) \
                        .eq("fs_div", fs_div) \
                        .limit(1).execute()
                    if res.data:
                        prev_row = {col: res.data[0].get(col) for col in FLOW_COLS}
                except Exception as e:
                    log.debug(f"[DB조회실패] {corp_name} {year} {prev_q}: {e}")

    row = convert_to_pure_quarter(row, prev_row)

    # 캐시 갱신
    if stock_code not in fin_cache:
        fin_cache[stock_code] = {}
    fin_cache[stock_code][(year, quarter, fs_div)] = {
        col: row.get(col) for col in FLOW_COLS
    }

    # 성장률 계산
    try:
        growth = calculate_growth_rates(fin_cache, row)
        row.update(growth)
    except Exception:
        pass

    # DB upsert
    sb.table("financials").upsert(
        [row], on_conflict="corp_code,bsns_year,quarter,fs_div"
    ).execute()

    return True


# ──────────────────────────────────────────────────────────────
#  메인 백필 함수
# ──────────────────────────────────────────────────────────────
def run_backfill(from_year: int, monitored_only: bool = False,
                 non_monitored_only: bool = False,
                 corp_names: list = None,
                 force: bool = False,
                 batch_save: int = 30,
                 check_only: bool = False):
    """
    누락된 재무 데이터를 분기별로 수집.

    Args:
        from_year         : 수집 시작 연도 (예: 2023)
        monitored_only    : True → 모니터링 종목만
        non_monitored_only: True → 비모니터링만
        corp_names        : 특정 종목명 리스트 (None=전체)
        force             : True → 이미 수집된 건도 재수집 (D&A 등 신규 컬럼 채울 때)
        batch_save        : 배치 저장 단위 (기본 30개)
    """
    if not DART_API_KEY or not SB_URL or not SB_SERVICE_KEY:
        log.error("DART_API_KEY, SB_URL, SB_SERVICE_KEY 환경변수 필요")
        sys.exit(1)

    dart = OpenDartReader(DART_API_KEY)
    sb   = _get_sb()

    quarters = _build_quarter_list(from_year)
    log.info(f"📅 수집 대상 분기: {quarters[0]} ~ {quarters[-1]} ({len(quarters)}개 분기)")

    # 회사 목록 로드
    companies = _load_companies(
        sb,
        monitored_only=monitored_only,
        non_monitored_only=non_monitored_only,
        corp_names=corp_names,
    )
    if not companies:
        log.error("수집 대상 종목 없음")
        return

    # 기존 수집 데이터 로드 (skip용) — force 모드에서는 스킵
    total_pairs = len(companies) * len(quarters)
    if force:
        log.info(f"⚡ --force 모드: 기존 수집 여부 무관하게 전체 재수집 ({total_pairs:,}건)")
        missing_pairs  = [(t, y, q) for t in companies for (y, q) in quarters]
        skipped_count  = 0
    else:
        existing = _load_existing(sb)
        missing_pairs = []
        for t in companies:
            for (y, q) in quarters:
                if (t["stock_code"], y, q) not in existing:
                    missing_pairs.append((t, y, q))
        skipped_count = total_pairs - len(missing_pairs)
        log.info(
            f"📊 전체 {total_pairs:,}개 페어 중 "
            f"이미수집 {skipped_count:,}개 스킵 → "
            f"누락 {len(missing_pairs):,}개 수집 예정"
        )

    if not missing_pairs:
        log.info("✅ 모두 수집 완료됨. 백필 불필요.")
        return

    if check_only:
        # 분기별 누락 현황 요약 출력 후 종료
        from collections import Counter
        by_quarter = Counter(f"{y} {q}" for _, y, q in missing_pairs)
        print("\n" + "="*60)
        print("📊 비모니터링 종목 재무 수집 현황 (누락)")
        print("="*60)
        for qtr in sorted(by_quarter):
            print(f"  ❌ {qtr}: 누락 {by_quarter[qtr]:,}개")
        print("="*60)
        print(f"  총 누락: {len(missing_pairs):,}건  |  이미수집: {skipped_count:,}건")
        print("  수집하려면 --check-only 제거 후 재실행")
        return

    # 기존 재무 캐시 로드 (누적→단독 변환 + 성장률 계산용)
    log.info("📦 재무 캐시 로드 중...")
    fin_cache = build_fin_cache(sb)
    log.info(f"📦 캐시 로드 완료: {sum(len(v) for v in fin_cache.values()):,}건")

    # 수집 실행
    ok_count   = 0
    fail_count = 0
    skip_count = 0  # DART에 데이터 없는 경우

    start_time = time.time()

    for idx, (t, year, quarter) in enumerate(missing_pairs, 1):
        corp_name = t["corp_name"]
        progress  = f"[{idx:,}/{len(missing_pairs):,}]"

        # 진행률 + 예상 잔여 시간 (100건마다 표시)
        if idx % 100 == 0 or idx <= 5:
            elapsed  = time.time() - start_time
            rate     = idx / elapsed if elapsed > 0 else 1
            remaining= (len(missing_pairs) - idx) / rate
            h, m = divmod(int(remaining), 3600)
            m, s = divmod(m, 60)
            log.info(
                f"⏱  {progress} 진행률 {idx/len(missing_pairs)*100:.1f}% | "
                f"잔여 예상 {h}시간 {m}분 ({rate:.1f}건/초)"
            )

        try:
            success = _collect_and_save(dart, sb, fin_cache, t, year, quarter)
            if success:
                ok_count += 1
                log.info(f"  ✅ {progress} {corp_name} {year} {quarter}")
            else:
                skip_count += 1
                log.debug(f"  ⬜ {progress} {corp_name} {year} {quarter} (DART 데이터 없음)")
        except Exception as e:
            fail_count += 1
            log.error(f"  ❌ {progress} {corp_name} {year} {quarter}: {e}")
            # 오류 시 1회 재시도
            try:
                time.sleep(2)
                success = _collect_and_save(dart, sb, fin_cache, t, year, quarter)
                if success:
                    ok_count   += 1
                    fail_count -= 1
                    log.info(f"  ✅ [재시도 성공] {corp_name} {year} {quarter}")
            except Exception as e2:
                log.error(f"  ❌ [재시도 실패] {corp_name} {year} {quarter}: {e2}")

        # DART API 속도 제한 방지 (0.12초 → 분당 ~500건)
        time.sleep(0.12)

    elapsed_total = time.time() - start_time
    h, m = divmod(int(elapsed_total), 3600)
    m, s = divmod(m, 60)

    log.info(
        f"\n{'='*60}\n"
        f"✅ 백필 완료 — 소요 {h}시간 {m}분 {s}초\n"
        f"   수집성공:   {ok_count:,}건\n"
        f"   DART없음:   {skip_count:,}건\n"
        f"   실패:       {fail_count:,}건\n"
        f"   기존스킵:   {skipped_count:,}건\n"
        f"{'='*60}"
    )


# ──────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="재무 데이터 과거분 백필 수집기")
    parser.add_argument(
        "--from-year", type=int, default=2023,
        help="수집 시작 연도 (기본: 2023)"
    )
    parser.add_argument(
        "--monitored-only", action="store_true",
        help="모니터링 종목만 (is_monitored=True)"
    )
    parser.add_argument(
        "--non-monitored-only", action="store_true",
        help="비모니터링 종목만 (active=True, is_monitored=False)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="전체 상장사 (monitored + non-monitored)"
    )
    parser.add_argument(
        "--corp-name", type=str, nargs="+",
        help="특정 종목명 (예: --corp-name 삼성전자 SK하이닉스)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="이미 수집된 건도 재수집 (D&A 등 신규 컬럼 채울 때 사용)"
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="수집 없이 누락 현황만 출력"
    )
    args = parser.parse_args()

    # 모드 결정
    if args.corp_name:
        monitored_only     = False
        non_monitored_only = False
        log.info(f"📌 지정 종목 모드: {args.corp_name}")
    elif args.all:
        monitored_only     = False
        non_monitored_only = False
        log.info("📌 전체 상장사 모드")
    elif args.non_monitored_only:
        monitored_only     = False
        non_monitored_only = True
    else:
        # 기본: 모니터링 종목만
        monitored_only     = True
        non_monitored_only = False

    run_backfill(
        from_year=args.from_year,
        monitored_only=monitored_only,
        non_monitored_only=non_monitored_only,
        corp_names=args.corp_name,
        force=args.force,
        check_only=args.check_only,
    )
