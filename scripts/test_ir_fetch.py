#!/usr/bin/env python3
"""
IR자료 수집 테스트 스크립트
────────────────────────────
DART API에서 기업설명회/IR자료 공시를 가져와
PDF 첨부파일을 @batiarchive로 전송하는 흐름을 검증합니다.

실행:
    source ~/.env
    python3 test_ir_fetch.py [--days 3] [--dry-run]
"""
import argparse
import io
import logging
import sys
import time
import zipfile
from datetime import date, timedelta
from io import BytesIO

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────────────────────
try:
    from config import DART_API_KEY, TELEGRAM_BOT_TOKEN
except ImportError:
    log.error("config.py 없음 — DART_API_KEY, TELEGRAM_BOT_TOKEN 확인")
    sys.exit(1)

DART_BASE   = "https://opendart.fss.or.kr/api"
TARGET_CHAT = "@batiarchive"   # 테스트 전송 채널

# IR 관련 공시 키워드
IR_KEYWORDS = [
    "기업설명회",
    "투자설명회",
    "NDR",
    "IR자료",
    "기업IR",
]

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})


# ══════════════════════════════════════════════════════════════
# 1. DART 공시 목록 조회
# ══════════════════════════════════════════════════════════════

def fetch_dart_list(bgn_de: str, end_de: str) -> list:
    """
    DART에서 전체 공시 목록 조회 후 IR 키워드 필터링.
    pblntf_ty 없이 전체 조회 (기타공시 포함).
    """
    results = []
    page = 1

    while True:
        params = {
            "crtfc_key": DART_API_KEY,
            "bgn_de":    bgn_de,
            "end_de":    end_de,
            "page_no":   page,
            "page_count": 100,
        }
        try:
            r = session.get(f"{DART_BASE}/list.json", params=params, timeout=15)
            data = r.json()
        except Exception as e:
            log.error(f"DART list 조회 오류: {e}")
            break

        if data.get("status") != "000":
            log.info(f"DART list 종료: {data.get('message')}")
            break

        items = data.get("list", [])
        for item in items:
            nm = item.get("report_nm", "")
            if any(kw in nm for kw in IR_KEYWORDS):
                results.append(item)

        if len(items) < 100:
            break
        page += 1
        time.sleep(0.3)

    log.info(f"IR 관련 공시 {len(results)}건 발견 ({bgn_de} ~ {end_de})")
    return results


# ══════════════════════════════════════════════════════════════
# 2. DART document.json → ZIP → PDF/HWP 추출
# ══════════════════════════════════════════════════════════════

def fetch_document_files(rcpt_no: str) -> list[tuple[str, BytesIO]]:
    """
    document.json API로 ZIP 다운로드 → (파일명, BytesIO) 리스트 반환.
    PDF/HWP 파일만 추출.
    """
import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

    url = f"{DART_BASE}/document.json"
    params = {"crtfc_key": DART_API_KEY, "rcpNo": rcpt_no}

    try:
        r = session.get(url, params=params, timeout=30, stream=True)
        r.raise_for_status()

        # Content-Type 확인
        ct = r.headers.get("Content-Type", "")
        if "application/zip" not in ct and "octet-stream" not in ct:
            log.warning(f"예상치 못한 Content-Type: {ct}")

        zip_buf = BytesIO(r.content)
        if not zipfile.is_zipfile(zip_buf):
            log.warning(f"ZIP 파일이 아님 (rcpNo={rcpt_no})")
            return []

        zip_buf.seek(0)
        results = []
        with zipfile.ZipFile(zip_buf) as zf:
            names = zf.namelist()
            log.info(f"  ZIP 내 파일 목록: {names}")

            for name in names:
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext in ("pdf", "hwp", "hwpx"):
                    file_buf = BytesIO(zf.read(name))
                    results.append((name, file_buf))
                    log.info(f"  → 추출: {name} ({file_buf.getbuffer().nbytes:,} bytes)")

        return results

    except Exception as e:
        log.error(f"document.json 오류 (rcpNo={rcpt_no}): {e}")
        return []


# ══════════════════════════════════════════════════════════════
# 3. 텔레그램 전송
# ══════════════════════════════════════════════════════════════

def send_tg_doc(chat_id: str, file_buf: BytesIO, filename: str, caption: str) -> bool:
    file_buf.seek(0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        r = session.post(url, data={
            "chat_id":    chat_id,
            "caption":    caption[:1024],
            "parse_mode": "HTML",
        }, files={"document": (filename, file_buf)}, timeout=60)
        ok = r.json().get("ok", False)
        if not ok:
            log.warning(f"TG 전송 실패: {r.json().get('description')}")
        return ok
    except Exception as e:
        log.error(f"TG 전송 오류: {e}")
        return False


def send_tg_text(chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = session.post(url, json={
            "chat_id":    chat_id,
            "text":       text[:4096],
            "parse_mode": "HTML",
        }, timeout=15)
        return r.json().get("ok", False)
    except Exception as e:
        log.error(f"TG 메시지 오류: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# 4. 메인
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="IR자료 수집 테스트")
    parser.add_argument("--days",    type=int, default=3,     help="조회 기간 (기본 3일)")
    parser.add_argument("--dry-run", action="store_true",     help="텔레그램 전송 생략")
    parser.add_argument("--limit",   type=int, default=3,     help="최대 처리 건수")
    args = parser.parse_args()

    end_de = date.today().strftime("%Y%m%d")
    bgn_de = (date.today() - timedelta(days=args.days)).strftime("%Y%m%d")

    log.info(f"== IR자료 수집 테스트 ({bgn_de} ~ {end_de}) ==")

    disclosures = fetch_dart_list(bgn_de, end_de)
    if not disclosures:
        log.info("IR 관련 공시 없음")
        return

    processed = 0
    for item in disclosures:
        if processed >= args.limit:
            break

        corp  = item.get("corp_name", "")
        nm    = item.get("report_nm", "")
        rcpno = item.get("rcept_no", "")
        dt_   = item.get("rcept_dt", "")

        log.info(f"\n[{processed+1}] {corp} — {nm} ({dt_})")
        log.info(f"    rcpNo: {rcpno}")

        # ZIP 다운 + 파일 추출
        files = fetch_document_files(rcpno)

        if not files:
            log.info("  → PDF/HWP 없음 — 텍스트 링크만 전송")
            if not args.dry_run:
                link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpno}"
                msg = (
                    f"📋 <b>[IR자료] {corp}</b>\n"
                    f"{nm} ({dt_})\n"
                    f"🔗 <a href='{link}'>공시 원문</a>"
                )
                ok = send_tg_text(TARGET_CHAT, msg)
                log.info(f"  텍스트 전송: {'✅' if ok else '❌'}")
        else:
            for filename, file_buf in files:
                ext = filename.rsplit(".", 1)[-1].upper()
                caption = (
                    f"📋 <b>[IR자료 / {ext}] {corp}</b>\n"
                    f"{nm} ({dt_})"
                )
                log.info(f"  → {'[DRY-RUN] ' if args.dry_run else ''}전송: {filename}")
                if not args.dry_run:
                    ok = send_tg_doc(TARGET_CHAT, file_buf, filename, caption)
                    log.info(f"  전송 결과: {'✅' if ok else '❌'}")
                    time.sleep(1)

        processed += 1
        time.sleep(0.5)

    log.info(f"\n== 완료: {processed}건 처리 ==")


if __name__ == "__main__":
    main()
