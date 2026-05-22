"""
collect_short.py
────────────────
KRX 데이터포털 → 당일 공매도 비중 수집 → short_selling_history 테이블 저장

수집 데이터 (종목별, 일별):
  short_ratio  : 당일 공매도 거래 비중 (%) — 당일 전체 거래량 대비 공매도 거래량
  short_volume : 당일 공매도 거래량 (주)

대상: companies.is_monitored=True 종목만 (약 300개)

KRX API:
  - 전체 종목 한 번에 조회 (OTP 방식)
  - bld: dbms/MDC/STAT/standard/MDCSTAT30101 (공매도 거래 현황, 당일 비중)
  - 응답: ISU_SRT_CD(종목코드), ACML_SELN_PBMN(공매도거래량), SRT_RT(공매도비중%)

실행:
  python collect_short.py              # 오늘
  python collect_short.py --date 20260510
  python collect_short.py --backfill 005930 000660  # 특정 종목 과거 데이터 수집
"""

import os
import sys
import time
import logging
from datetime import date, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv
load_dotenv()

try:
    from supabase import create_client
except ImportError:
    print("pip install supabase 필요")
    sys.exit(1)

log = logging.getLogger(__name__)

SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")

KRX_OTP_URL  = "http://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
KRX_DATA_URL = "http://data.krx.co.kr/comm/fileDn/download_excel.cmd"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://data.krx.co.kr/",
}


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


def fetch_short_selling(trd_date: str) -> list[dict]:
    """
    KRX에서 당일 공매도 거래 현황 전체 종목 조회.
    bld: dbms/MDC/STAT/standard/MDCSTAT30101 (공매도 거래 현황)

    KRX download_excel.cmd는 Excel 바이너리를 반환함.
    → openpyxl로 파싱.

    반환:
        [{'stock_code': str, 'short_ratio': float, 'short_volume': int}, ...]
        데이터 없음(공휴일 등): []
    """
    import io
    import openpyxl

    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1: OTP 발급
    otp_params = {
        "bld":         "dbms/MDC/STAT/standard/MDCSTAT30101",
        "name":        "fileDown",
        "filetype":    "xls",          # Excel 형식으로 요청
        "url":         "dbms/MDC/STAT/standard/MDCSTAT30101",
        "trdDd":       trd_date,
        "mktCd":       "ALL",
        "isuCd":       "",
        "sortCd":      "SRT_RT",
        "money":       "1",
        "csvxls_isNo": "false",
    }

    try:
        otp_res = session.post(KRX_OTP_URL, data=otp_params, timeout=15)
        otp_res.raise_for_status()
        token = otp_res.text.strip()
        if not token:
            log.warning(f"⚠️ [공매도] OTP 빈 응답 ({trd_date})")
            return []
    except Exception as e:
        log.error(f"❌ [공매도] OTP 요청 실패 ({trd_date}): {e}")
        return []

    time.sleep(0.5)

    # Step 2: Excel 파일 다운로드
    try:
        data_res = session.post(
            KRX_DATA_URL,
            data={"code": token},
            timeout=30,
        )
        data_res.raise_for_status()

        content_type = data_res.headers.get("Content-Type", "")
        log.debug(f"📉 [공매도] {trd_date} Content-Type: {content_type}")

        # 빈 응답 또는 HTML 에러 페이지 (공휴일 등)
        if len(data_res.content) < 1000:
            log.debug(f"📉 [공매도] {trd_date} 응답 너무 작음 ({len(data_res.content)}bytes) — 공휴일 또는 수집 전")
            return []

    except Exception as e:
        log.error(f"❌ [공매도] 데이터 다운로드 실패 ({trd_date}): {e}")
        return []

    # Step 3: Excel 파싱
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data_res.content), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        log.error(f"❌ [공매도] Excel 파싱 실패 ({trd_date}): {e}")
        log.debug(f"응답 앞부분: {data_res.content[:200]}")
        return []

    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        log.debug(f"📉 [공매도] {trd_date} 데이터 없음")
        wb.close()
        return []

    # 헤더 행에서 컬럼 인덱스 찾기
    header = [str(c).strip() if c else "" for c in rows[0]]
    log.debug(f"📉 [공매도] {trd_date} 헤더: {header}")

    # KRX Excel 헤더 매핑 (버전별로 이름이 다를 수 있음)
    COL_CODE   = _find_col(header, ["단축코드", "종목코드", "ISU_SRT_CD"])
    COL_RATIO  = _find_col(header, ["공매도비중", "비중", "SRT_RT"])
    COL_VOLUME = _find_col(header, ["공매도거래량", "거래량", "ACML_SELN_PBMN"])

    if COL_CODE is None:
        log.error(f"❌ [공매도] 종목코드 컬럼을 찾을 수 없음. 헤더: {header}")
        wb.close()
        return []

    results = []
    for row in rows[1:]:
        if not row or not row[COL_CODE]:
            continue

        code = str(row[COL_CODE]).strip().zfill(6)  # 6자리 패딩

        ratio = None
        if COL_RATIO is not None and row[COL_RATIO] is not None:
            try:
                ratio = float(str(row[COL_RATIO]).replace(",", "").replace("%", ""))
            except (ValueError, TypeError):
                pass

        volume = None
        if COL_VOLUME is not None and row[COL_VOLUME] is not None:
            try:
                volume = int(str(row[COL_VOLUME]).replace(",", "").split(".")[0])
            except (ValueError, TypeError):
                pass

        if ratio is None:
            continue

        results.append({
            "stock_code":   code,
            "short_ratio":  ratio,
            "short_volume": volume,
        })

    wb.close()
    log.debug(f"📉 [공매도] {trd_date}: {len(results)}개 종목 파싱")
    return results


def _find_col(header: list, candidates: list) -> int | None:
    """헤더 리스트에서 후보 컬럼명 중 하나를 찾아 인덱스 반환"""
    for i, h in enumerate(header):
        for c in candidates:
            if c in h:
                return i
    return None


def _get_monitored_codes(sb) -> set:
    """companies.is_monitored=True 종목코드 세트 반환"""
    codes = set()
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
                codes.add(code)
        if len(res.data) < 1000:
            break
        page += 1
    return codes


def _upsert_batch(sb, records: list[dict]) -> int:
    """short_selling_history에 배치 upsert. 저장 건수 반환."""
    if not records:
        return 0
    saved = 0
    batch_size = 200
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        try:
            sb.table("short_selling_history") \
              .upsert(batch, on_conflict="stock_code,base_date") \
              .execute()
            saved += len(batch)
        except Exception as e:
            log.error(f"❌ [공매도] upsert 실패: {e}")
    return saved


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

    sb = create_client(SB_URL, SB_SERVICE_KEY)
    monitored = _get_monitored_codes(sb)
    if not monitored:
        log.warning("⚠️ [공매도] 모니터링 종목 없음")
        return 0, 0

    log.info(f"📉 [공매도] 모니터링 종목: {len(monitored)}개")

    all_records = fetch_short_selling(trd_date)
    if not all_records:
        return 0, 0

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
    sb = create_client(SB_URL, SB_SERVICE_KEY)

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

    for i, trd_date in enumerate(dates):
        # 이 날짜에 모든 대상 종목의 데이터가 이미 있으면 스킵
        if all((code, trd_date) in existing for code in stock_codes):
            log.debug(f"  {trd_date}: 전체 기존 데이터 존재 — 스킵")
            continue

        all_records = fetch_short_selling(trd_date)

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


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
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
