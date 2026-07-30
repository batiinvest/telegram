"""
collect_qtr_consensus.py
────────────────────────
네이버 증권 분기 재무 API → 분기 컨센서스(예상 영업익/매출/순이익) 스냅샷

어닝 서프라이즈 판정용. 실적 발표 전에 시장 컨센서스를 미리 저장해 둔다
(발표되면 네이버가 같은 셀을 실제값으로 덮어써 컨센이 사라지므로).

소스: https://m.stock.naver.com/api/stock/{code}/finance/quarter
  - trTitleList의 isConsensus="Y" 분기 = 아직 미발표 = 컨센서스(증권사 평균)
  - rowList의 '영업이익'/'매출액'/'당기순이익' 값 (억원, 문자열·콤마·'-'=결측)

저장: quarterly_consensus (stock_code, quarter='YYYYMM', op_consensus, ...)
  - PK (stock_code, quarter) upsert — 매일 실행해 최신 컨센으로 갱신.
  - 발표 후엔 네이버가 다음 분기를 Y로 노출 → 다른 quarter 키로 저장,
    직전 분기 컨센 스냅샷은 보존됨.

실행:
  python collect_qtr_consensus.py          # 전 종목 스냅샷
  python collect_qtr_consensus.py 259960   # 특정 종목 (디버그)
"""

import sys
import time
import logging
from datetime import date

import requests

from logger_config import get_logger
from collect_utils import batch_upsert
from db_client import get_supabase_client
from config import COMPANY_CODES

log = get_logger(__name__)

from dotenv import load_dotenv
load_dotenv()

_NAVER_URL = "https://m.stock.naver.com/api/stock/{code}/finance/quarter"
_HDRS = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}


def _parse_eok(s):
    """네이버 억원 문자열 → float. '-'/''/None → None."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_quarter_consensus(code: str) -> dict | None:
    """네이버 분기 API에서 컨센서스(isConsensus='Y') 분기의 영업익/매출/순이익 추출.
    반환: {'quarter':'202606','op':float,'sales':float,'ni':float} (억원) 또는 None.
    영업이익 컨센이 없으면(커버리지 밖) None."""
    url = _NAVER_URL.format(code=code)
    r = requests.get(url, headers=_HDRS, timeout=10)
    r.raise_for_status()
    fi = (r.json() or {}).get("financeInfo") or {}
    titles = fi.get("trTitleList") or []
    cons = next((t for t in titles if t.get("isConsensus") == "Y"), None)
    if not cons or not cons.get("key"):
        return None
    key = cons["key"]
    rows = {row.get("title"): (row.get("columns") or {})
            for row in (fi.get("rowList") or [])}

    def _val(title):
        col = rows.get(title, {}).get(key) or {}
        return _parse_eok(col.get("value"))

    op = _val("영업이익")
    if op is None:
        return None
    return {"quarter": key, "op": op,
            "sales": _val("매출액"), "ni": _val("당기순이익")}


def run(codes: dict = None) -> int:
    """모니터링 종목 분기 컨센서스 스냅샷 → quarterly_consensus upsert. 반환: 커버 종목 수."""
    sb = get_supabase_client()
    targets = codes or COMPANY_CODES
    today = date.today().isoformat()

    records, covered, failed = [], 0, 0
    for name, code in targets.items():
        try:
            c = fetch_quarter_consensus(code)
        except Exception as e:
            failed += 1
            log.debug(f"[분기컨센] {code} {name} 조회 실패: {e}")
            time.sleep(0.2)
            continue
        if c:
            covered += 1
            records.append({
                "stock_code": code,
                "quarter": c["quarter"],
                "op_consensus": c["op"],
                "sales_consensus": c["sales"],
                "ni_consensus": c["ni"],
                "snapshot_date": today,
            })
        time.sleep(0.2)

    saved = 0
    if records:
        try:
            saved = batch_upsert(sb, "quarterly_consensus", records,
                                 "stock_code,quarter", chunk=100)
        except Exception as e:
            log.error(f"[분기컨센] 저장 실패 (quarterly_consensus 테이블 미생성?): {e}")
            return 0
    log.info(f"[분기컨센] 완료: 커버 {covered}/{len(targets)}종목, "
             f"{saved}행 upsert, 조회실패 {failed}")
    return covered


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        c = sys.argv[1]
        print(fetch_quarter_consensus(c))
        run({c: c})
    else:
        run()
