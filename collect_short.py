"""
collect_short.py
────────────────
KRX 데이터포털 → 당일 공매도 비중 수집 → short_selling_history 테이블 저장

수집 데이터 (종목별, 일별):
  short_ratio  : 당일 공매도 거래 비중 (%) — 당일 전체 거래량 대비 공매도 거래량
  short_volume : 당일 공매도 거래량 (주)

대상: companies.is_monitored=True 종목만 (약 300개)

KRX API (로그인 필요 — .env KRX_ID·KRX_PW, collect_active_etf.Krx 세션):
  - 시장별(코스피 STK·코스닥 KSQ) 전 종목 하루치 JSON
  - bld: dbms/MDC/STAT/srt/MDCSTAT30101 (전종목 공매도 거래)
  - 응답: ISU_CD(종목코드), CVSRTSELL_TRDVOL(공매도거래량), ACC_TRDVOL(거래량), TRDVOL_WT(거래량 대비 비중%)

실행:
  python collect_short.py              # 오늘
  python collect_short.py --date 20260510
  python collect_short.py --backfill 005930 000660  # 특정 종목 과거 데이터 수집
"""

import os
import sys
import time
from logger_config import get_logger
from collect_utils import batch_upsert, fetch_all_pages
log = get_logger(__name__)

from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

try:
    from db_client import get_supabase_client
except ImportError:
    print("pip install supabase 필요")
    sys.exit(1)


SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")


def _last_business_day(d: date) -> str:
    """주말이면 직전 금요일로 조정 후 YYYYMMDD 반환"""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _business_days_before(d: date, n: int) -> list[str]:
    """
    d 기준 과거 n거래일 날짜 리스트 반환 (주말 제외, d 포함).
    단순히 주말만 제외 (공휴일은 KRX 응답이 비어있으면 스킵).
    """
    result = []
    cur = d
    while len(result) < n:
        if cur.weekday() < 5:  # 월~금
            result.append(cur.strftime("%Y%m%d"))
        cur -= timedelta(days=1)
    return result


def _krx_session():
    """KRX 정보데이터시스템 로그인 세션(collect_active_etf와 같은 계정·방식)."""
    from collect_active_etf import Krx
    krx = Krx()
    krx.login()
    return krx


def fetch_short_selling(trd_date: str, krx=None) -> list[dict]:
    """
    KRX '전종목 공매도 거래' 하루치 — 코스피(STK)·코스닥(KSQ) 각각 JSON 1회.
    bld: dbms/MDC/STAT/srt/MDCSTAT30101

    [2026-10-27 교체] 예전엔 비로그인 OTP → 엑셀 다운로드였는데, KRX가 로그인을 요구하면서
    OTP가 'LOGOUT'·다운로드가 에러 페이지로 와서 'Excel 파싱 실패: File is not a zip file'로
    2026-06-12부터 수집이 0건이었다(잡은 성공으로 기록돼 4개월 넘게 몰랐다).
    로그인 세션(.env KRX_ID·KRX_PW)으로 받고, 통계 경로도 standard → srt로 옮겨졌다.

    반환:
        [{'stock_code': str, 'short_ratio': float(거래량 대비 %), 'short_volume': int}, ...]
        데이터 없음(휴장일 등): []
    """
    krx = krx or _krx_session()
    results = []
    for mkt in ("STK", "KSQ"):
        d = krx.get("dbms/MDC/STAT/srt/MDCSTAT30101", searchType="1", mktId=mkt, trdDd=trd_date,
                    inqCond="STMFRTSCIFDRFS", share="1", money="1", csvxls_isNo="false")
        for r in d.get("OutBlock_1") or []:
            code = (r.get("ISU_CD") or "").strip()
            try:
                ratio = float(str(r.get("TRDVOL_WT") or "").replace(",", ""))
                volume = int(str(r.get("CVSRTSELL_TRDVOL") or "0").replace(",", ""))
                traded = int(str(r.get("ACC_TRDVOL") or "0").replace(",", ""))
            except ValueError:
                continue
            if code and traded:          # 거래 없는 날(휴장일 등)은 전부 0으로 온다
                results.append({"stock_code": code, "short_ratio": ratio, "short_volume": volume})
        time.sleep(0.5)
    log.debug(f"📉 [공매도] {trd_date}: {len(results)}개 종목")
    return results


def _get_monitored_codes(sb) -> set:
    """companies.is_monitored=True 종목코드 세트 반환"""
    codes = set()
    for r in fetch_all_pages(sb.table("companies").select("code").eq("is_monitored", True)):
        code = (r.get("code") or "").split(".")[0]
        if code:
            codes.add(code)
    return codes


def _upsert_batch(sb, records: list[dict]) -> int:
    """short_selling_history에 배치 upsert. 저장 건수 반환."""
    return batch_upsert(sb, "short_selling_history", records, "stock_code,base_date", chunk=200)


def run(trd_date: str = None) -> tuple[int, int]:
    """
    당일 공매도 비중 수집 → short_selling_history upsert.
    모니터링 종목만 필터링해서 저장.

    Returns: (saved, skipped)
    """
    if not SB_URL or not SB_SERVICE_KEY:
        log.error("SB_URL, SB_SERVICE_KEY 환경변수 필요")
        return 0, 0

    if not trd_date:
        trd_date = _last_business_day(date.today())

    log.info(f"📉 [공매도] 당일 수집 시작 — {trd_date}")

    sb = get_supabase_client()
    monitored = _get_monitored_codes(sb)
    if not monitored:
        log.warning("⚠️ [공매도] 모니터링 종목 없음")
        return 0, 0

    log.info(f"📉 [공매도] 모니터링 종목: {len(monitored)}개")

    all_records = fetch_short_selling(trd_date)
    if not all_records:
        # 잡은 휴장일 가드(@_job holiday)라 여기 오면 영업일 — 0건은 장애로 올린다.
        # 예전엔 0을 돌려줘 '급증 종목 없음'으로만 보였고, 4개월간 수집 0건을 몰랐다.
        raise RuntimeError(f"[공매도] {trd_date} 수집 0건 — KRX 응답 없음(로그인·경로 확인)")

    # 모니터링 종목만 필터
    to_save = [
        {
            "stock_code": r["stock_code"],
            "base_date":  trd_date,
            "short_ratio":  r["short_ratio"],
            "short_volume": r["short_volume"],
        }
        for r in all_records
        if r["stock_code"] in monitored
    ]
    skipped = len(all_records) - len(to_save)

    saved = _upsert_batch(sb, to_save)
    log.info(f"📉 [공매도] 완료 — 저장 {saved}개 / 비모니터링 스킵 {skipped}개")
    return saved, skipped


def backfill(stock_codes: list[str], days: int = 252) -> int:
    """
    신규 모니터링 종목의 과거 공매도 데이터 백필.
    최대 days 거래일 (기본 252일 ≈ 1년) 소급 수집.

    KRX는 전체 종목을 날짜별로 한 번에 내려주므로,
    날짜별로 1회 요청 → 해당 종목만 필터링해서 저장.
    요청 간 0.5초 대기 (KRX rate limit 준수).

    Returns: 총 저장 건수
    """
    if not SB_URL or not SB_SERVICE_KEY:
        log.error("SB_URL, SB_SERVICE_KEY 환경변수 필요")
        return 0

    if not stock_codes:
        return 0

    target_set = set(stock_codes)
    sb = get_supabase_client()

    # 이미 데이터가 있는 (stock_code, base_date) 제외
    existing = set()
    if len(stock_codes) <= 50:  # 소수 종목이면 미리 조회해서 중복 방지
        try:
            res = sb.table("short_selling_history") \
                    .select("stock_code,base_date") \
                    .in_("stock_code", stock_codes) \
                    .execute()
            for r in (res.data or []):
                existing.add((r["stock_code"], r["base_date"]))
        except Exception:
            pass

    dates = _business_days_before(date.today(), days)
    log.info(
        f"📉 [공매도 백필] {len(stock_codes)}개 종목 × 최대 {len(dates)}거래일 "
        f"({dates[-1]} ~ {dates[0]})"
    )

    total_saved = 0
    empty_streak = 0  # 연속 빈 응답 (공휴일 연속 등) 카운트
    krx = _krx_session()   # 날짜마다 로그인하지 않도록 한 번만

    for i, trd_date in enumerate(dates):
        # 이 날짜에 모든 대상 종목의 데이터가 이미 있으면 스킵
        if all((code, trd_date) in existing for code in stock_codes):
            log.debug(f"  {trd_date}: 전체 기존 데이터 존재 — 스킵")
            continue

        all_records = fetch_short_selling(trd_date, krx)

        if not all_records:
            empty_streak += 1
            # 연속 5일 빈 응답이면 KRX 서버 문제 또는 데이터 없는 구간
            if empty_streak >= 5:
                log.warning(f"⚠️ [공매도 백필] {trd_date} 연속 빈 응답 {empty_streak}회 — 중단")
                break
            time.sleep(0.5)
            continue

        empty_streak = 0

        to_save = [
            {
                "stock_code": r["stock_code"],
                "base_date":  trd_date,
                "short_ratio":  r["short_ratio"],
                "short_volume": r["short_volume"],
            }
            for r in all_records
            if r["stock_code"] in target_set
            and (r["stock_code"], trd_date) not in existing
        ]

        if to_save:
            saved = _upsert_batch(sb, to_save)
            total_saved += saved
            log.info(f"  {trd_date}: {saved}건 저장 ({i+1}/{len(dates)})")
        else:
            log.debug(f"  {trd_date}: 대상 종목 데이터 없음")

        time.sleep(0.5)  # KRX rate limit

    log.info(f"📉 [공매도 백필] 완료 — 총 {total_saved}건 저장")
    return total_saved


def check_surge(sb, n_days: int = 5, multiplier: float = 2.0) -> list[dict]:
    """
    당일 공매도 비중이 최근 n_days 거래일 평균 대비 multiplier배 이상인 종목 탐지.

    short_selling_history 테이블에서 최신 base_date(당일) + 직전 n_days일 데이터를 읽어
    종목별로 평균 대비 급증 여부를 판단.

    Returns: 급증 종목 리스트 (today_ratio, avg_ratio, surge_ratio, corp_name 포함)
    """
    try:
        # 최신 날짜 조회
        date_res = sb.table("short_selling_history") \
                     .select("base_date") \
                     .order("base_date", desc=True) \
                     .limit(1).execute()
        if not (date_res.data):
            log.warning("[공매도급증] 데이터 없음")
            return []
        latest_date = date_res.data[0]["base_date"]

        # 최근 n_days+1거래일치 전체 조회 (당일 포함) — 달력 3주로 넉넉히 잡고 페이지네이션.
        # 예전 limit((n_days+1)*500)은 PostgREST 1,000행 상한에 잘려 311종목×6일(1,866행) 중
        # 3일치만 와서 '비교 데이터 3일 미만'으로 전 종목이 빠졌다.
        ld = str(latest_date).replace("-", "")[:8]
        cutoff = (date(int(ld[:4]), int(ld[4:6]), int(ld[6:])) - timedelta(days=21)).isoformat()
        rows = fetch_all_pages(
            sb.table("short_selling_history")
              .select("stock_code,base_date,short_ratio")
              .gte("base_date", cutoff)
              .order("base_date", desc=True)
        )
        if not rows:
            return []

        # 종목별 날짜 정렬된 리스트 구성
        from collections import defaultdict
        by_code = defaultdict(list)
        for r in rows:
            by_code[r["stock_code"]].append(r)
        # 날짜 내림차순 정렬 보장
        for code in by_code:
            by_code[code].sort(key=lambda x: x["base_date"], reverse=True)

        # 종목명 조회 (market_data 최신)
        # PostgREST 1000행 한도 -> 전체 상장종목(~2,600개) 페이지네이션
        name_rows = fetch_all_pages(
            sb.table("market_data").select("stock_code,corp_name").eq("base_date", latest_date)
        )
        name_map = {r["stock_code"]: r["corp_name"] for r in name_rows}

        surges = []
        for code, hist in by_code.items():
            if not hist or hist[0]["base_date"] != latest_date:
                continue  # 당일 데이터 없는 종목 스킵
            today_ratio = hist[0]["short_ratio"]
            if today_ratio is None or today_ratio < 1.0:
                continue  # 비중 1% 미만 노이즈 제외

            prev = [r["short_ratio"] for r in hist[1:n_days + 1] if r["short_ratio"] is not None]
            if len(prev) < 3:
                continue  # 비교 데이터 3일 미만이면 스킵

            avg_ratio = sum(prev) / len(prev)
            if avg_ratio < 0.5:
                continue  # 평균이 너무 낮으면 배율 의미 없음

            if today_ratio >= avg_ratio * multiplier:
                surges.append({
                    "stock_code":  code,
                    "corp_name":   name_map.get(code, code),
                    "today_ratio": round(today_ratio, 2),
                    "avg_ratio":   round(avg_ratio, 2),
                    "surge_ratio": round(today_ratio / avg_ratio, 1),
                })

        surges.sort(key=lambda x: x["surge_ratio"], reverse=True)
        log.info(f"📉 [공매도급증] 탐지 {len(surges)}개 (기준: {n_days}일 평균 × {multiplier}배)")
        return surges

    except Exception as e:
        log.error(f"❌ [공매도급증] check_surge 실패: {e}")
        return []


def format_surge_msg(surges: list[dict], trd_date: str = None, multiplier: float = 2.0) -> str:
    """공매도 급증 알림 메시지 포맷 — run_all·CLI 공용 (템플릿 단일화)."""
    lines = [
        f"📉 <b>[공매도 급증 알림]</b> {trd_date or '오늘'}\n"
        f"5거래일 평균 대비 {multiplier:.0f}배↑ 급증 종목 ({len(surges)}개)\n"
    ]
    for i, s in enumerate(surges[:10], 1):
        lines.append(
            f"{i}. <b>{s['corp_name']}</b>({s['stock_code']})\n"
            f"   오늘 <b>{s['today_ratio']}%</b> / 5일평균 {s['avg_ratio']}% "
            f"→ <b>{s['surge_ratio']}배</b>"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KRX 공매도 거래 비중 수집")
    parser.add_argument("--date", type=str, default=None,
                        help="조회일 YYYYMMDD (기본: 오늘 또는 직전 거래일)")
    parser.add_argument("--backfill", nargs="+", metavar="CODE",
                        help="과거 데이터 백필할 종목코드 (예: 005930 000660)")
    parser.add_argument("--days", type=int, default=252,
                        help="백필 거래일 수 (기본: 252일)")
    args = parser.parse_args()

    if args.backfill:
        backfill(args.backfill, days=args.days)
    else:
        run(trd_date=args.date)
