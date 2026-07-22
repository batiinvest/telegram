"""
collect_credit_balance.py
─────────────────────────
금융투자협회(KOFIA freesis) → 신용공여 잔고 추이 → credit_balance_history 저장

수집 데이터 (시장 전체, 일별, 단위: 백만원):
  loan_total / loan_kospi / loan_kosdaq          : 신용거래융자 잔고
  stock_loan_total / stock_loan_kospi / _kosdaq  : 신용거래대주 잔고
  subscription_loan                              : 청약자금대출
  secured_loan                                   : 예탁증권담보융자

KOFIA API (freesis.kofia.or.kr — exbuilder SPA 내부 JSON API, 2026-07 실측):
  POST /meta/getMetaDataList.do
  body: {"dmSearch":{"tmpV1":"D","tmpV45":시작일,"tmpV46":종료일,
                     "tmpV40":"1000000","OBJ_NM":"STATSCU0100000070BO"}}
  응답: {"ds1":[{"TMPV1":"YYYYMMDD","TMPV2":융자전체,...,"TMPV9":담보융자}]}
  ※ tmpV40=1000000 → 백만원 단위. 자료는 결제일 기준.
  ※ 발표 시점(2026-07-22 실측): 직전 영업일분이 당일 "오후"에 올라온다.
     07-22 10:30 조회 시 최신=07-20 / 같은 날 18:18 조회 시 최신=07-21.
     → 오전 수집은 항상 하루 묵은 값만 받으므로 일일 잡은 19:00(+10:30 보정).
  ※ 구 openapi/service.do는 2026 사이트 개편으로 폐기 — 위 경로가 현행.

실행:
  python3 collect_credit_balance.py                     # 최근 14일 (일일 잡)
  python3 collect_credit_balance.py --backfill 20240101 # 지정일부터 백필
"""

import sys
import time
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
from collect_utils import batch_upsert
from db_client import get_supabase_client

log = get_logger(__name__)

KOFIA_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
OBJ_NM = "STATSCU0100000070BO"  # 신용공여 잔고 추이
TABLE = "credit_balance_history"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://freesis.kofia.or.kr/stat/FreeSIS.do",
    "Origin": "https://freesis.kofia.or.kr",
}

# KOFIA 응답 TMPV → 테이블 컬럼 (그리드 헤더 순서 기준)
FIELD_MAP = {
    "TMPV2": "loan_total",
    "TMPV3": "loan_kospi",
    "TMPV4": "loan_kosdaq",
    "TMPV5": "stock_loan_total",
    "TMPV6": "stock_loan_kospi",
    "TMPV7": "stock_loan_kosdaq",
    "TMPV8": "subscription_loan",
    "TMPV9": "secured_loan",
}


def fetch_credit_balance(start_ymd: str, end_ymd: str) -> list:
    """KOFIA 신용공여 잔고 조회 (YYYYMMDD 구간) → upsert 레코드 리스트"""
    payload = {"dmSearch": {
        "tmpV1": "D", "tmpV45": start_ymd, "tmpV46": end_ymd,
        "tmpV40": "1000000", "OBJ_NM": OBJ_NM,
    }}
    r = requests.post(KOFIA_URL, json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    rows = r.json().get("ds1") or []
    records = []
    for row in rows:
        ymd = str(row.get("TMPV1") or "")
        if len(ymd) != 8 or not ymd.isdigit():
            continue
        rec = {"base_date": "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:])}
        for tmpv, col in FIELD_MAP.items():
            v = row.get(tmpv)
            rec[col] = int(v) if isinstance(v, (int, float)) else None
        records.append(rec)
    return records


def run(days: int = 14) -> int:
    """일일 수집 — 최근 days일 윈도 upsert (발표 지연·휴장 여유분 포함, 멱등)"""
    end = date.today()
    start = end - timedelta(days=days)
    records = fetch_credit_balance(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if not records:
        log.warning("[신용잔고] KOFIA 응답에 데이터 없음")
        return 0
    sb = get_supabase_client()
    n = batch_upsert(sb, TABLE, records, "base_date")
    dates = sorted(r["base_date"] for r in records)
    log.info(f"[신용잔고] {dates[0]}~{dates[-1]} {n}건 upsert")
    return n


def backfill(start_ymd: str) -> int:
    """지정일부터 오늘까지 1년 단위 청크로 백필"""
    sb = get_supabase_client()
    start = datetime.strptime(start_ymd, "%Y%m%d").date()
    end = date.today()
    total = 0
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=364), end)
        records = fetch_credit_balance(cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d"))
        n = batch_upsert(sb, TABLE, records, "base_date")
        log.info(f"[신용잔고 백필] {cur}~{chunk_end}: {len(records)}건 조회 → {n}건 upsert")
        total += n
        cur = chunk_end + timedelta(days=1)
        time.sleep(1)
    return total


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--backfill":
        print(f"백필 완료: {backfill(sys.argv[2])}건")
    else:
        print(f"수집 완료: {run()}건")
