#!/usr/bin/env python3
"""
KIND IR자료실 다운로드 가능 여부 테스트
https://kind.krx.co.kr

실행:
    python3 test_kind_ir.py
"""
import logging
import sys
from io import BytesIO

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

try:
    from config import TELEGRAM_BOT_TOKEN
except ImportError:
    TELEGRAM_BOT_TOKEN = None

TARGET_CHAT = "@batiarchive"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://kind.krx.co.kr/",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "ko-KR,ko;q=0.9",
})

# ── 1. KIND IR자료실 목록 조회 ─────────────────────────────────
def fetch_kind_ir_list(page: int = 1) -> list:
    """KIND IR자료실 목록 조회 (POST)"""
    url = "https://kind.krx.co.kr/ir/irnotice.do"
    data = {
        "method":       "searchIrNoticeList",
        "currentPage":  str(page),
        "pageSize":     "20",
        "orderMode":    "0",
        "orderStat":    "D",
        "forward":      "irnotice_list",
        "searchCodeType": "",
        "searchCorpName": "",
        "repIsuSrtCd":  "",
        "startDt":      "",
        "endDt":        "",
    }
    try:
        r = session.post(url, data=data, timeout=15)
        log.info(f"KIND 목록 응답: {r.status_code} / Content-Type: {r.headers.get('Content-Type','')}")
        log.info(f"응답 길이: {len(r.content)} bytes")

        if r.status_code != 200:
            log.error(f"HTTP {r.status_code}")
            return []

        # HTML 파싱 시도
        soup = BeautifulSoup(r.text, "html.parser")

        # 테이블 행 추출 시도
        items = []
        rows = soup.select("table tbody tr") or soup.select("tr")
        log.info(f"테이블 행 수: {len(rows)}")

        for row in rows[:5]:  # 처음 5개만
            cols = row.find_all("td")
            if cols:
                text = " | ".join(c.get_text(strip=True) for c in cols)
                log.info(f"  행: {text[:120]}")

                # 다운로드 링크 찾기
                links = row.find_all("a")
                for a in links:
                    href = a.get("href", "")
                    onclick = a.get("onclick", "")
                    if href or onclick:
                        log.info(f"    링크: href={href!r}, onclick={onclick!r}")
                items.append({"text": text, "row": str(row)[:200]})

        # JSON 응답인지도 확인
        try:
            j = r.json()
            log.info(f"JSON 응답: {str(j)[:300]}")
        except Exception:
            pass

        return items

    except Exception as e:
        log.error(f"KIND 목록 조회 오류: {e}")
        return []


# ── 2. KIND 다운로드 URL 패턴 테스트 ──────────────────────────
def test_download_patterns():
    """알려진 KIND 다운로드 URL 패턴들 테스트"""
    patterns = [
        # 공식 IR자료실
        "https://kind.krx.co.kr/ir/irnotice.do?method=searchIrNoticeMain",
        "https://kind.krx.co.kr/ir/irnotice.do",
        # 기업공시채널
        "https://kind.krx.co.kr/disclosure/irnotice.do",
        # IR 자료 직접 다운로드 패턴 (예시)
        "https://kind.krx.co.kr/common/irfiledwn.do?method=download&fid=1",
    ]

    for url in patterns:
        try:
            r = session.get(url, timeout=10, allow_redirects=True)
            log.info(f"GET {url}")
            log.info(f"  → {r.status_code} / {r.headers.get('Content-Type','')[:60]}")
            if r.status_code == 200 and len(r.content) < 5000:
                log.info(f"  → 내용: {r.text[:200]}")
        except Exception as e:
            log.info(f"  → 오류: {e}")


# ── 3. 메인 ───────────────────────────────────────────────────
def main():
    log.info("=== KIND IR자료실 테스트 ===\n")

    log.info("--- 1. URL 패턴 테스트 ---")
    test_download_patterns()

    log.info("\n--- 2. 목록 조회 테스트 ---")
    items = fetch_kind_ir_list()

    if not items:
        log.info("목록 조회 실패 또는 빈 결과")
        log.info("\n▶ KIND IR자료실은 별도 세션/인증이 필요하거나 구조가 다를 수 있습니다.")
    else:
        log.info(f"\n{len(items)}건 조회됨")


if __name__ == "__main__":
    main()
