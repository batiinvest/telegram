#!/usr/bin/env python3
"""
kind_ir.py  —  KIND IR자료실 수집 → @batiarchive 전송
────────────────────────────────────────────────────
KIND(한국거래소 기업공시채널)의 IR자료실에서 새로 업로드된 IR자료(PDF)를
다운로드하여 텔레그램 @batiarchive 채널로 전송합니다.

핵심 동작 방식:
  - KIND "일자" 컬럼 = IR 개최 예정일 (업로드일 ≠ 일자)
  - 기업이 미래 IR 자료를 오늘 미리 업로드할 수 있음
  - 따라서 날짜 필터 대신 irSeq(자동 증가 업로드 순번) 기준으로 신규 자료 탐지
  - 마지막으로 처리한 irSeq를 Supabase에 저장 → 다음 실행 시 그 이후 것만 수집

사용법:
    from kind_ir import run_kind_ir_job
    run_kind_ir_job()           # 신규 IR자료 수집 (일반 실행)
    run_kind_ir_job(lookback=50)  # 최근 50건까지 소급 수집 (초기 세팅)
"""

import json
import logging
import re
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
log.setLevel(logging.INFO)   # config.py 의 basicConfig(WARNING) 보다 먼저 고정

# ── 상수 ───────────────────────────────────────────────────────
KIND_BASE        = "https://kind.krx.co.kr"
KIND_IR_MAIN     = f"{KIND_BASE}/corpgeneral/irschedule.do?method=searchIRScheduleMain&gubun=iRMaterials"
KIND_IR_LIST_URL = f"{KIND_BASE}/corpgeneral/irschedule.do"
TARGET_CHAT      = "@batiarchive"

_SB_LAST_SEQ_KEY = "kind_ir_last_seq"   # 마지막으로 처리한 irSeq (int as str)
_SB_SENT_KEY     = "kind_ir_sent"       # 전송 완료 irSeq 목록 (JSON array)
_MAX_SENT_KEEP   = 300                  # 보관할 최대 irSeq 수


# ══════════════════════════════════════════════════════════════
#  세션
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
#  Supabase — 마지막 irSeq / 전송 이력 관리
# ══════════════════════════════════════════════════════════════

def _load_state() -> tuple[int, set]:
    """
    Supabase에서 (last_seq, sent_set) 로드.
    last_seq: 마지막으로 처리한 irSeq (없으면 0)
    sent_set: 전송 완료 irSeq 집합
    """
    last_seq = 0
    sent_set = set()
    try:
        from supabase_bridge import bridge
        raw_seq  = bridge.get_config(_SB_LAST_SEQ_KEY, "0")
        last_seq = int(raw_seq) if raw_seq else 0
        raw_sent = bridge.get_config(_SB_SENT_KEY, "[]")
        sent_set = set(str(x) for x in json.loads(raw_sent))
    except Exception as e:
        log.debug(f"[KIND IR] state 로드 실패: {e}")
    return last_seq, sent_set


def _save_state(last_seq: int, sent_set: set):
    """처리한 최대 irSeq와 전송 이력을 Supabase에 저장"""
    try:
        from supabase_bridge import bridge as _bridge
        client = _bridge._get_client()
        if not client:
            return
        # 최대 _MAX_SENT_KEEP개만 보관 (오래된 것 제거)
        lst = sorted(int(x) for x in sent_set if str(x).isdigit())[-_MAX_SENT_KEEP:]
        client.table("app_config").upsert(
            {"key": _SB_LAST_SEQ_KEY, "value": str(last_seq),
             "description": "KIND IR 마지막 처리 irSeq"},
            on_conflict="key",
        ).execute()
        client.table("app_config").upsert(
            {"key": _SB_SENT_KEY, "value": json.dumps(lst),
             "description": "KIND IR 전송 완료 irSeq 목록"},
            on_conflict="key",
        ).execute()
    except Exception as e:
        log.debug(f"[KIND IR] state 저장 실패: {e}")


# ══════════════════════════════════════════════════════════════
#  1. KIND IR자료 목록 조회 (날짜 필터 없이 최근 N건)
# ══════════════════════════════════════════════════════════════

def fetch_ir_list_recent(session: requests.Session, pages: int = 3) -> list[dict]:
    """
    KIND IR자료실 최근 목록을 조회합니다 (날짜 필터 없음).
    irSeq 기준으로 최신 업로드 순으로 반환합니다.

    Args:
        pages: 조회할 페이지 수 (1페이지 = 40건, 기본 3페이지 = 최근 120건)

    반환: [{'ir_seq': str, 'corp': str, 'date': str, 'category': str,
             'filename': str, 'pdf_path': str}, ...]
    """
    # 1) 세션 쿠키 획득
    session.headers.update({"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        session.get(KIND_IR_MAIN, timeout=15)
    except Exception as e:
        log.warning(f"[KIND IR] 세션 초기화 실패: {e}")

    # 2) AJAX POST
    session.headers.update({
        "Referer":          KIND_IR_MAIN,
        "X-Requested-With": "XMLHttpRequest",
        "Accept":           "text/html, */*; q=0.01",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    })

    today    = date.today()
    # 날짜 필터는 IR 개최 예정일 기준 → 최대한 넓게 설정
    # (오늘 업로드했지만 미래 일자인 자료까지 모두 포함)
    from_dt  = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    to_dt    = (today + timedelta(days=365)).strftime("%Y-%m-%d")

    results = []
    for page in range(1, pages + 1):
        data = {
            "method":          "searchIRMaterialsSub",
            "forward":         "searchirmaterials_sub",
            "currentPageSize": "40",
            "pageIndex":       str(page),
            "searchCodeType":  "",
            "repIsuSrtCd":     "",
            "irSeq":           "",
            "searchCorpName":  "",
            "resoroomType":    "",
            "marketType":      "",
            "searchName":      "",
            "kosdaqSegment":   "",
            "title":           "",
            "fromDate":        from_dt,
            "toDate":          to_dt,
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

            pdf_link = row.find(
                "a",
                href=lambda h: h and h.startswith("/external/dst/irReference/")
            )
            if not pdf_link:
                continue

            # irSeq 추출
            ir_seq = ""
            detail_a = row.find("a", onclick=lambda o: o and "fnDetailView" in o if o else False)
            if detail_a:
                m = re.search(r"fnDetailView\('(\d+)'", detail_a.get("onclick", ""))
                if m:
                    ir_seq = m.group(1)
            if not ir_seq:
                pdf_path = pdf_link.get("href", "")
                m = re.search(r"/irReference/(\d+)/", pdf_path)
                ir_seq = m.group(1) if m else ""

            # 시장 구분 (코스피/코스닥/코넥스)
            market_img = tds[1].find("img")
            market = market_img.get("alt", "") if market_img else ""

            # 종목코드 (companysummary_open('15371') → 015371)
            corp_a = tds[1].find("a", onclick=lambda o: o and "companysummary_open" in o if o else False)
            stock_code = ""
            if corp_a:
                m2 = re.search(r"companysummary_open\('(\d+)'\)", corp_a.get("onclick", ""))
                if m2:
                    stock_code = m2.group(1).zfill(6)

            item = {
                "ir_seq":     ir_seq,
                "corp":       tds[1].get_text(strip=True),
                "date":       tds[2].get_text(strip=True),   # IR 개최 예정일
                "category":   tds[3].get_text(strip=True),
                "filename":   pdf_link.get_text(strip=True),
                "pdf_path":   pdf_link.get("href", ""),
                "market":     market,       # 코스피 / 코스닥 / 코넥스
                "stock_code": stock_code,   # 종목코드 6자리
            }
            results.append(item)
            page_items += 1

        log.info(f"[KIND IR] page={page}, 수집={page_items}건 (누적 {len(results)}건)")

        if page_items < 40:
            break  # 마지막 페이지
        time.sleep(0.3)

    return results


# ══════════════════════════════════════════════════════════════
#  2. PDF 다운로드
# ══════════════════════════════════════════════════════════════

def download_pdf(session: requests.Session, pdf_path: str) -> BytesIO | None:
    """KIND IR자료 PDF 다운로드"""
    url = urljoin(KIND_BASE, pdf_path)
    try:
        r = session.get(url, timeout=60, headers={
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

def _send_text(chat_id: str, text: str) -> bool:
    """텍스트 메시지 전송 (요약/알림용)"""
    if not TELEGRAM_BOT_TOKEN:
        log.warning("[KIND IR] TELEGRAM_BOT_TOKEN 없음 — _send_text 스킵")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    log.info(f"[KIND IR] 텍스트 전송 시도: chat_id={chat_id}, len={len(text)}")
    try:
        r = requests.post(url, data={
            "chat_id":                  chat_id,
            "text":                     text[:4096],
            "parse_mode":               "HTML",
            "disable_web_page_preview": "true",
        }, timeout=30)
        resp = r.json()
        ok = resp.get("ok", False)
        if not ok:
            log.warning(f"[KIND IR] TG 텍스트 전송 실패: {resp}")
        return ok
    except Exception as e:
        log.error(f"[KIND IR] TG 텍스트 전송 오류: {e}")
        return False


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

def run_kind_ir_job(
    chat_id:  str  = TARGET_CHAT,
    dry_run:  bool = False,
    lookback: int  = 3,       # 조회할 페이지 수 (1page=40건)
    reset:    bool = False,   # True면 last_seq 무시하고 모두 재처리
):
    """
    KIND IR자료 수집 → 텔레그램 전송 메인 함수

    irSeq 기반으로 신규 업로드만 탐지합니다.
    "일자"(IR 개최 예정일)와 무관하게 오늘 새로 업로드된 자료를 전부 잡습니다.

    Args:
        chat_id:  전송할 텔레그램 채널 (기본 @batiarchive)
        dry_run:  True면 텔레그램 전송 생략
        lookback: 조회할 페이지 수 (기본 3페이지 = 최근 120건)
        reset:    True면 last_seq 무시 (재처리 / 초기 세팅)
    """
    log.info(f"[KIND IR] === 수집 시작 (lookback={lookback}pages) ===")

    session              = _make_session()
    last_seq, sent_set   = _load_state()

    if reset:
        last_seq = 0
        sent_set = set()
        log.info("[KIND IR] reset=True — last_seq·sent_set 초기화")

    log.info(f"[KIND IR] 마지막 처리 irSeq={last_seq}, 전송이력={len(sent_set)}개")

    # 목록 조회
    items = fetch_ir_list_recent(session, pages=lookback)
    if not items:
        log.info("[KIND IR] 수집된 IR자료 없음")
        return

    # irSeq 기준 내림차순 정렬 (최신 → 오래된 순)
    items.sort(key=lambda x: int(x["ir_seq"]) if x["ir_seq"].isdigit() else 0, reverse=True)

    # 신규 항목만 필터 (last_seq 초과 & 미전송)
    new_items = [
        it for it in items
        if it["ir_seq"].isdigit()
        and int(it["ir_seq"]) > last_seq
        and it["ir_seq"] not in sent_set
    ]

    log.info(
        f"[KIND IR] 전체 {len(items)}건 중 신규(irSeq>{last_seq}) {len(new_items)}건"
    )

    if not new_items:
        log.info("[KIND IR] 신규 자료 없음")
        # last_seq는 갱신하지 않음
        return

    # 오래된 것부터 전송 (순서 보장)
    new_items.sort(key=lambda x: int(x["ir_seq"]))

    # ── 전송 전 요약 메시지 ────────────────────────────────────
    today_str_hdr = date.today().strftime("%Y-%m-%d")
    summary_lines = [
        f"📋 <b>[KIND IR자료] {today_str_hdr}</b> (총 {len(new_items)}건)\n"
    ]
    for it in new_items:
        dt = it["date"]
        future_mark = " 📅" if dt > today_str_hdr else ""
        mkt = f" [{it['market']}]" if it.get("market") else ""
        summary_lines.append(f"• {it['corp']}{mkt} — {dt}{future_mark}")
    summary_msg = "\n".join(summary_lines)

    if dry_run:
        log.info(f"  [DRY-RUN] 요약 메시지:\n{summary_msg}")
    else:
        ok = _send_text(chat_id, summary_msg)
        log.info(f"[KIND IR] 요약 메시지 전송: {'OK' if ok else 'FAIL'}")
        time.sleep(1)
    # ──────────────────────────────────────────────────────────

    new_sent    = set(sent_set)
    max_seq_ok  = last_seq
    sent_ok     = 0

    for item in new_items:
        ir_seq     = item["ir_seq"]
        corp       = item["corp"]
        dt_str     = item["date"]   # IR 개최 예정일
        category   = item["category"]
        filename   = item["filename"]
        pdf_path   = item["pdf_path"]
        market     = item.get("market", "")
        stock_code = item.get("stock_code", "")

        log.info(f"[KIND IR] 처리: irSeq={ir_seq} {corp} (IR일자={dt_str})")

        buf = download_pdf(session, pdf_path)
        if buf is None:
            log.warning(f"[KIND IR] PDF 다운로드 실패 — 스킵: {corp}")
            # 다운로드 실패는 재시도 가능성을 위해 sent에 추가하지 않음
            continue

        # 캡션 구성
        future_tag   = " 📅" if dt_str > today_str_hdr else ""
        # 해시태그용 회사명 (공백 제거)
        corp_tag     = re.sub(r'\s+', '', corp)

        caption = (
            f"📋<b>{corp} IR자료</b>\n"
            f"IR 개최일: {dt_str}{future_tag}\n"
            f"#IR자료 #{corp_tag}"
        )

        # 전송용 파일명: 기업명_IR_날짜.pdf
        safe_corp = re.sub(r'[\\/:*?"<>|]', '', corp)   # 파일명 금지 문자 제거
        send_filename = f"{safe_corp}_IR_{dt_str.replace('-', '')[2:]}.pdf"

        if dry_run:
            log.info(f"  [DRY-RUN] 전송 생략: {send_filename} ({buf.getbuffer().nbytes:,}B)")
            new_sent.add(ir_seq)
            max_seq_ok = max(max_seq_ok, int(ir_seq))
            sent_ok += 1
        else:
            ok = _send_doc(chat_id, buf, send_filename, caption)
            if ok:
                log.info(f"  [OK] {corp}: {filename}")
                new_sent.add(ir_seq)
                max_seq_ok = max(max_seq_ok, int(ir_seq))
                sent_ok += 1
            else:
                log.warning(f"  [FAIL] {corp}: {filename}")

        time.sleep(1)  # 텔레그램 레이트 리밋

    # 최대 성공 irSeq와 전송 이력 저장
    if max_seq_ok > last_seq:
        _save_state(max_seq_ok, new_sent)

    log.info(
        f"[KIND IR] === 완료: 신규 전송 {sent_ok}건 / "
        f"전체 신규 {len(new_items)}건 ==="
    )


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # config.py 가 basicConfig 를 WARNING 레벨로 먼저 호출하므로
    # no-op 이 되는 경우를 대비해 root 레벨을 명시적으로 재설정
    logging.getLogger().setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="KIND IR자료 수집 → 텔레그램 전송")
    parser.add_argument("--dry-run",  action="store_true",         help="텔레그램 전송 생략")
    parser.add_argument("--chat-id",  default=TARGET_CHAT,          help="전송 채널")
    parser.add_argument("--lookback", type=int,   default=3,        help="조회 페이지 수 (1page=40건)")
    parser.add_argument("--reset",    action="store_true",          help="last_seq 무시하고 전체 재처리")
    args = parser.parse_args()

    run_kind_ir_job(
        chat_id=args.chat_id,
        dry_run=args.dry_run,
        lookback=args.lookback,
        reset=args.reset,
    )
