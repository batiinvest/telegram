#!/usr/bin/env python3
"""
네이버 증권 과거 리포트 일괄 전송 스크립트
지정한 날짜 범위의 산업분석·기업분석 리포트를 수집해 텔레그램 채널로 발송합니다.

사용법:
  python3 backfill_reports.py                             # 기본: 최근 30일
  python3 backfill_reports.py --days 60                   # 최근 60일
  python3 backfill_reports.py --from 2026-04-01 --to 2026-05-24
  python3 backfill_reports.py --from 2026-05-01 --dry-run # 전송 없이 목록만 출력
  python3 backfill_reports.py --skip-history              # 이미 보낸 것도 재전송
"""

import sys, time, logging, argparse
from datetime import date, timedelta
from urllib.parse import urlencode
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

try:
    import stock_api as sa
except ImportError as e:
    log.error(f"stock_api 임포트 실패: {e}")
    sys.exit(1)

try:
    from managers import HistoryManager
except ImportError as e:
    log.error(f"managers 임포트 실패: {e}")
    sys.exit(1)


# ──────────────────────────────────────────────────
def daterange(from_dt: date, to_dt: date):
    """from_dt ~ to_dt 사이 평일(월~금)만 yield"""
    cur = from_dt
    while cur <= to_dt:
        if cur.weekday() < 5:   # 0=월 … 4=금
            yield cur
        cur += timedelta(days=1)


def fetch_reports_for_date(date_str: str, page_type: str,
                           history: HistoryManager, skip_history: bool) -> list:
    """특정 날짜·유형의 리포트를 파싱해 리스트로 반환"""
    base_url = sa.NAVER_REPORT_URLS[page_type]
    reports, page = [], 1

    while True:
        try:
            params = {
                "searchType": "writeDate",
                "writeFromDate": date_str,
                "writeToDate":   date_str,
                "page": page,
            }
            res  = sa._session.get(f"{base_url}?{urlencode(params)}", timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")

            table = soup.find("table", {"class": "type_1"})
            if not table:
                break

            rows = table.find_all("tr")
            if not rows:
                break

            page_has_data = False
            for row in rows:
                data = sa._parse_report_row(row, base_url, page_type)
                if not data:
                    continue
                page_has_data = True
                _, file_name, _ = data
                if not skip_history and history.contains(file_name):
                    log.debug(f"   [SKIP 중복] {file_name}")
                    continue
                reports.append(data)

            total_pages = sa._get_total_pages(soup)
            if not page_has_data or page >= total_pages:
                break

            page += 1
            time.sleep(0.3)

        except Exception as e:
            log.error(f"크롤 오류 ({page_type} {date_str} p.{page}): {e}")
            break

    return reports


def backfill(from_dt: date, to_dt: date,
             chat_id: str, dry_run: bool, skip_history: bool):
    history    = HistoryManager("sent_reports.txt", max_len=5000)
    total_sent = 0

    log.info(f"📆 백필 범위: {from_dt} ~ {to_dt}  →  {chat_id}")
    log.info(f"   dry_run={dry_run}  skip_history={skip_history}")

    for cur_date in daterange(from_dt, to_dt):
        date_str = cur_date.strftime("%Y-%m-%d")
        log.info(f"\n===== {date_str} =====")

        for page_type in ["산업분석", "기업분석"]:
            reports = fetch_reports_for_date(date_str, page_type, history, skip_history)

            if not reports:
                log.info(f"   {page_type}: 없음")
                continue

            log.info(f"   {page_type}: {len(reports)}건")

            if dry_run:
                for _, file_name, _ in reports:
                    log.info(f"   [DRY] {file_name}")
                continue

            # 요약 메시지 (30개씩 분할)
            header     = f"📑 <b>[{date_str}] {page_type} 리포트</b> (총 {len(reports)}개)\n\n"
            chunk_size = 30
            for i in range(0, len(reports), chunk_size):
                chunk = reports[i:i + chunk_size]
                lines = []
                if i == 0:
                    lines.append(header)
                for j, item in enumerate(chunk):
                    clean = item[1].replace(".pdf", "").replace("_", " ")
                    lines.append(f"{i+j+1}. {clean}")
                sa.send_telegram(chat_id, "\n".join(lines))
                time.sleep(0.5)

            # PDF 개별 전송
            for pdf_url, file_name, _ in reports:
                pdf_buf    = sa._fetch_pdf_file(pdf_url)
                target_doc = pdf_buf if pdf_buf else pdf_url
                caption    = sa._safe_caption(file_name)

                sa._send_telegram_doc(chat_id, target_doc, file_name, caption)
                history.add(file_name)   # 중복 방지용 기록
                total_sent += 1
                log.info(f"   ✅ 전송: {file_name}")
                time.sleep(1.5)          # 텔레그램 플러딩 방지

        time.sleep(2)   # 날짜 간 딜레이

    log.info(f"\n🎉 완료 — 총 {total_sent}건 전송 (dry_run={dry_run})")


# ──────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="네이버 리포트 과거 날짜 백필")
    p.add_argument("--from",         dest="from_date",
                   help="시작일 YYYY-MM-DD  (기본: --days 일 전)")
    p.add_argument("--to",           dest="to_date",
                   help="종료일 YYYY-MM-DD  (기본: 어제)")
    p.add_argument("--days",         type=int, default=30,
                   help="최근 N일  (--from/--to 미지정 시, 기본 30)")
    p.add_argument("--chat-id",      dest="chat_id",
                   default=sa.NAVER_REPORT_CHAT_ID,
                   help=f"대상 채널 ID  (기본: {sa.NAVER_REPORT_CHAT_ID})")
    p.add_argument("--dry-run",      action="store_true",
                   help="전송 없이 수집 목록만 출력")
    p.add_argument("--skip-history", action="store_true",
                   help="이미 보낸 리포트도 재전송")
    return p.parse_args()


if __name__ == "__main__":
    args    = parse_args()
    today   = date.today()

    from_dt = (date.fromisoformat(args.from_date)
               if args.from_date else today - timedelta(days=args.days))
    to_dt   = (date.fromisoformat(args.to_date)
               if args.to_date else today - timedelta(days=1))

    if from_dt > to_dt:
        print("오류: 시작일이 종료일보다 늦습니다.")
        sys.exit(1)

    backfill(from_dt, to_dt, args.chat_id, args.dry_run, args.skip_history)
