#!/usr/bin/env python3
"""
kind_ir.py  —  KIND IR자료실 수집 → @batiarchive 전송
────────────────────────────────────────────────────
KIND(한국거래소 기업공시채널)의 IR자료실에서 오늘 등록된 IR자료(PDF)를
다운로드하여 텔레그램 @batiarchive 채널로 전송합니다.

사용법:
    from kind_ir import run_kind_ir_job
    run_kind_ir_job()           # 오늘자 IR자료 수집
    run_kind_ir_job(days=3)     # 최근 3일치 수집 (초기 세팅 등)
"""

import json
import logging
import time
from datetime import date, timedelta
from io import BytesIO
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from config import TELEGRAM_BOT_TOKEN
except ImportError:
    TELEGRAM_BOT_TOKEN = None

log = logging.getLogger(__name__)

# ── 상수 ───────────────────────────────────────────────────────
KIND_BASE        = "https://kind.krx.co.kr"
KIND_IR_MAIN     = f"{KIND_BASE}/corpgeneral/irschedule.do?method=searchIRScheduleMain&gubun=iRMaterials"
KIND_IR_LIST_URL = f"{KIND_BASE}/corpgeneral/irschedule.do"
TARGET_CHAT      = "@batiarchive"

# 이미 전송한 irSeq를 추적 (재시작 간 메모리 공유 X → Supabase 사용)
_SB_SENT_KEY     = "kind_ir_sent"   # app_config 키 (JSON array)
_MAX_SENT_KEEP   = 200              # 보관할 최대 irSeq 수


# ══════════════════════════════════════════════════════════════
#  세션 공유 (재사용)
# ══════════════════════════════════════════════════════════════

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    return s


# ══════════════════════════════════════════════════════════════
#  중복 방지 — Supabase app_config 기반
# ══════════════════════════════════════════════════════════════

def _load_sent_set() -> set:
    """Supabase에서 이미 전송한 irSeq 목록 로드"""
    try:
        from supabase_bridge import bridge
        raw = bridge.get_config(_SB_SENT_KEY, "[]")
        return set(json.loads(raw))
    except Exception as e:
        log.debug(f"[KIND IR] sent set 로드 실패: {e}")
        return set()


def _save_sent_set(sent: set):
    """전송 완료된 irSeq 목록을 Supabase에 저장 (최대 _MAX_SENT_KEEP개 보관)"""
    try:
        from supabase_bridge import bridge as _bridge
        client = _bridge._get_client()
        if not client:
            return
        lst = sorted(int(x) for x in sent)[-_MAX_SENT_KEEP:]
        client.table("app_config").upsert(
            {"key": _SB_SENT_KEY, "value": json.dumps(lst), "description": "KIND IR 전송 완료 irSeq 목록"},
            on_conflict="key",
        ).execute()
    except Exception as e:
        log.debug(f"[KIND IR] sent set 저장 실패: {e}")


# ══════════════════════════════════════════════════════════════
#  1. KIND IR자료 목록 조회
# ══════════════════════════════════════════════════════════════

def fetch_ir_list(session: requests.Session, from_dt: str, to_dt: str) -> list[dict]:
    """
    KIND IR자료실에서 지정 기간의 IR자료 목록을 가져옵니다.
    반환: [{'ir_seq': str, 'corp': str, 'date': str, 'category': str,
             'filename': str, 'pdf_path': str}, ...]
    """
    # 1) 세션 쿠키 획득 (메인 페이지 GET)
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    try:
        session.get(KIND_IR_MAIN, timeout=15)
    except Exception as e:
        log.warning(f"[KIND IR] 세션 초기화 실패: {e}")

    # 2) AJAX POST로 목록 조회
    session.headers.update({
        "Referer":          KIND_IR_MAIN,
        "X-Requested-With": "XMLHttpRequest",
        "Accept":           "text/html, */*; q=0.01",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    })

    results = []
    page = 1
    while True:
        data = {
            "method":        "searchIRMaterialsSub",
            "forward":       "searchirmaterials_sub",
            "currentPageSize": "40",
            "pageIndex":     str(page),
            "searchCodeType": "",
            "repIsuSrtCd":   "",
            "irSeq":         "",
            "searchCorpName": "",
            "resoroomType":  "",
            "marketType":    "",
            "searchName":    "",
            "kosdaqSegment": "",
            "title":         "",
            "fromDate":      from_dt,
            "toDate":        to_dt,
        }
        try:
            r = session.post(KIND_IR_LIST_URL, data=data, timeout=15)
            r.raise_for_status()
        except Exception as e:
            log.error(f"[KIND IR] 목록 조회 오류 (page={page}): {e}")
            break

        soup = BeautifulSoup(r.content, "html.parser", from_encoding="euc-kr")
        rows = soup.select("table tbody tr") or soup.select("tr")

        page_items = 0
        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 4:
                continue

            # PDF 링크
            pdf_link = row.find(
                "a",
                href=lambda h: h and h.startswith("/external/dst/irReference/")
            )
            if not pdf_link:
                continue

            # irSeq 추출 (fnDetailView('18652','2'))
            import re
            ir_seq = ""
            detail_a = row.find("a", onclick=lambda o: o and "fnDetailView" in o if o else False)
            if detail_a:
                m = re.search(r"fnDetailView\('(\d+)'", detail_a.get("onclick", ""))
                if m:
                    ir_seq = m.group(1)

            if not ir_seq:
                # pdf_path에서 추출
                pdf_path = pdf_link.get("href", "")
                m = re.search(r"/irReference/(\d+)/", pdf_path)
                ir_seq = m.group(1) if m else ""

            item = {
                "ir_seq":   ir_seq,
                "corp":     tds[1].get_text(strip=True),
                "date":     tds[2].get_text(strip=True),
                "category": tds[3].get_text(strip=True),
                "filename": pdf_link.get_text(strip=True),
                "pdf_path": pdf_link.get("href", ""),
            }
            results.append(item)
            page_items += 1

        log.info(f"[KIND IR] page={page}, 수집={page_items}건 (누적 {len(results)}건)")

        if page_items < 40:
            break  # 마지막 페이지
        page += 1
        time.sleep(0.5)

    return results


# ══════════════════════════════════════════════════════════════
#  2. PDF 다운로드
# ══════════════════════════════════════════════════════════════

def download_pdf(session: requests.Session, pdf_path: str) -> BytesIO | None:
    """KIND IR자료 PDF 다운로드"""
    url = urljoin(KIND_BASE, pdf_path)
    try:
        r = session.get(url, timeout=60, stream=True, headers={
            "Referer": KIND_IR_MAIN,
            "Accept":  "application/pdf,*/*",
        })
        r.raise_for_status()
        buf = BytesIO(r.content)
        if buf.getbuffer().nbytes < 1000:
            log.warning(f"[KIND IR] 파일 너무 작음 ({buf.getbuffer().nbytes}B): {url}")
            return None
        log.info(f"[KIND IR] 다운로드 완료: {url} ({buf.getbuffer().nbytes:,} bytes)")
        return buf
    except Exception as e:
        log.error(f"[KIND IR] PDF 다운로드 오류: {e} — {url}")
        return None


# ══════════════════════════════════════════════════════════════
#  3. 텔레그램 전송
# ══════════════════════════════════════════════════════════════

def _send_doc(chat_id: str, buf: BytesIO, filename: str, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("[KIND IR] TELEGRAM_BOT_TOKEN 없음")
        return False
    buf.seek(0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        r = requests.post(url, data={
            "chat_id":    chat_id,
            "caption":    caption[:1024],
            "parse_mode": "HTML",
        }, files={"document": (filename, buf, "application/pdf")}, timeout=120)
        ok = r.json().get("ok", False)
        if not ok:
            log.warning(f"[KIND IR] TG 전송 실패: {r.json().get('description')}")
        return ok
    except Exception as e:
        log.error(f"[KIND IR] TG 전송 오류: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  4. 메인 실행 함수
# ══════════════════════════════════════════════════════════════

def run_kind_ir_job(days: int = 1, chat_id: str = TARGET_CHAT, dry_run: bool = False):
    """
    KIND IR자료 수집 → @batiarchive 전송 메인 함수

    Args:
        days:    조회 기간 (기본 1일 = 오늘만)
        chat_id: 전송할 텔레그램 채널 (기본 @batiarchive)
        dry_run: True면 텔레그램 전송 생략 (테스트용)
    """
    today    = date.today()
    from_dt  = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    to_dt    = today.strftime("%Y-%m-%d")

    log.info(f"[KIND IR] === 수집 시작: {from_dt} ~ {to_dt} ===")

    session  = _make_session()
    sent_set = _load_sent_set()

    # 목록 조회
    items = fetch_ir_list(session, from_dt, to_dt)
    if not items:
        log.info("[KIND IR] 수집된 IR자료 없음")
        return

    log.info(f"[KIND IR] 총 {len(items)}건 발견, 이미 전송={len(sent_set)}개 추적 중")

    new_sent = set(sent_set)
    sent_ok  = 0
    skipped  = 0

    for item in items:
        ir_seq   = item["ir_seq"]
        corp     = item["corp"]
        dt_str   = item["date"]
        category = item["category"]
        filename = item["filename"]
        pdf_path = item["pdf_path"]

        if ir_seq in sent_set:
            log.debug(f"[KIND IR] 스킵(이미 전송): irSeq={ir_seq} {corp}")
            skipped += 1
            continue

        log.info(f"[KIND IR] 처리: irSeq={ir_seq} {corp} ({dt_str})")

        # PDF 다운로드
        buf = download_pdf(session, pdf_path)
        if buf is None:
            log.warning(f"[KIND IR] PDF 다운로드 실패, 링크만 전송: {corp}")
            kind_url = f"{KIND_BASE}/corpgeneral/irschedule.do?method=searchIRScheduleMain&gubun=iRMaterials"
            caption = (
                f"📋 <b>[KIND IR자료] {corp}</b>\n"
                f"{category} ({dt_str})\n"
                f"🔗 <a href='{kind_url}'>KIND IR자료실</a>"
            )
            if not dry_run:
                # 링크 메시지는 전송하지 않음 (파일 없으면 스킵)
                pass
            new_sent.add(ir_seq)
            continue

        # 캡션 작성
        caption = (
            f"📋 <b>[KIND IR자료]</b>\n"
            f"<b>{corp}</b>\n"
            f"{category} | {dt_str}"
        )

        if dry_run:
            log.info(f"  [DRY-RUN] 전송 생략: {filename} ({buf.getbuffer().nbytes:,}B)")
            new_sent.add(ir_seq)
            sent_ok += 1
        else:
            ok = _send_doc(chat_id, buf, filename, caption)
            if ok:
                log.info(f"  [OK] {corp}: {filename}")
                new_sent.add(ir_seq)
                sent_ok += 1
            else:
                log.warning(f"  [FAIL] {corp}: {filename}")

        time.sleep(1)  # 텔레그램 레이트 리밋 방지

    # 전송 목록 저장
    _save_sent_set(new_sent)

    log.info(
        f"[KIND IR] === 완료: 신규 전송 {sent_ok}건 / 스킵 {skipped}건 "
        f"(전체 {len(items)}건) ==="
    )


# ══════════════════════════════════════════════════════════════
#  CLI 직접 실행
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="KIND IR자료 수집 → @batiarchive 전송")
    parser.add_argument("--days",    type=int, default=1,       help="조회 기간 (기본 1일)")
    parser.add_argument("--dry-run", action="store_true",        help="텔레그램 전송 생략")
    parser.add_argument("--chat-id", default=TARGET_CHAT,        help="전송 채널")
    args = parser.parse_args()

    run_kind_ir_job(days=args.days, chat_id=args.chat_id, dry_run=args.dry_run)
