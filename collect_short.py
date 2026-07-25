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
from logger_config import get_logger
from collect_utils import batch_upsert, fetch_all_pages
log = get_logger(__name__)

from datetime import date, timedelta

import requests
from dotenv import load_dotenv
load_dotenv()

try:
    from db_client import get_supabase_client
except ImportError:
    print("pip install supabase 필요")
    sys.exit(1)


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

        # 최근 n_days+1일치 전체 조회 (당일 포함)
        rows_res = sb.table("short_selling_history") \
                     .select("stock_code,base_date,short_ratio") \
                     .order("base_date", desc=True) \
                     .limit((n_days + 1) * 500).execute()
        rows = rows_res.data or []
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


def run_and_alert(trd_date: str = None, telegram_token: str = None,
                  chat_id: str = None, n_days: int = 5, multiplier: float = 2.0) -> int:
    """
    공매도 수집 → 급증 알림 일괄 처리.
    run_all.py의 job_short_surge에서 호출.

    Returns: 알림 발송 건수
    """
    if not SB_URL or not SB_SERVICE_KEY:
        log.error("SB_URL, SB_SERVICE_KEY 환경변수 필요")
        return 0

    sb = get_supabase_client()

    # 1. 당일 데이터 수집
    run(trd_date)

    # 2. 급증 탐지
    surges = check_surge(sb, n_days=n_days, multiplier=multiplier)
    if not surges:
        return 0

    # 3. 텔레그램 알림 (stock_api.send_telegram 사용)
    if not telegram_token or not chat_id:
        log.info(f"📉 [공매도급증] 텔레그램 미설정 — 콘솔 출력만")
        for s in surges[:10]:
            log.info(f"  {s['corp_name']}({s['stock_code']}) "
                     f"오늘 {s['today_ratio']}% / 5일평균 {s['avg_ratio']}% "
                     f"→ {s['surge_ratio']}배")
        return len(surges)

    # 상위 10개 알림 포맷 (run_all·CLI 공용 포매터)
    msg = format_surge_msg(surges, trd_date, multiplier)

    try:
        import requests as _req
        _req.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        log.info(f"📉 [공매도급증] 텔레그램 발송 완료 ({len(surges)}건)")
    except Exception as e:
        log.error(f"❌ [공매도급증] 텔레그램 발송 실패: {e}")

    return len(surges)


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
