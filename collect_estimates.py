"""
collect_estimates.py
────────────────────
KIS 종목추정실적(estimate-perform) → 미래 매출/영업이익 추정치 수집

수집 데이터 (종목별, 연간 5개 시점 = 과거 3개년 확정 + 미래 2개년 추정):
  consensus_estimates : 추정치 스냅샷 이력 (est_date=애널리스트 추정일 단위로 누적)
  estimate_revisions  : est_date 변경 감지 시 미래 연도 매출/영업이익 변화폭 기록
                        → 프론트 '오늘의 시황 > 전망 탭'(추정치 상향 종목)의 데이터 소스

KIS API:
  - /uapi/domestic-stock/v1/quotations/estimate-perform (TR HHKST668300C0, 국내주식-187)
  - 한국투자증권 리서치 추정치 (컨센서스 평균 아님). 커버리지 ≈ 모니터링 312종목 중 91종목.
  - 단위 규칙 (2026-07-03 실측): 금액=억원 그대로, 비율(YoY/PER/ROE 등)=응답값/10 이 %.
  - 미커버 종목은 output1.sht_cd 가 빈 문자열 → 스킵.

실행:
  python collect_estimates.py          # 모니터링 전 종목 수집
  python collect_estimates.py 005930   # 특정 종목만 (디버그, DB 저장 포함)
"""

import sys
import time
import logging
from logger_config import get_logger
from collect_utils import batch_upsert, fetch_all_pages
log = get_logger(__name__)

from dotenv import load_dotenv
load_dotenv()

from db_client import get_supabase_client
from managers import kis_auth
from config import COMPANY_CODES

API_PATH = "quotations/estimate-perform"   # kis_auth.kis_get이 공통 prefix 부착
TR_ID = "HHKST668300C0"

# output2 행 순서 (실측): 매출액, 매출YoY, 영업이익, 영업이익YoY, 순이익, 순이익YoY
# output3 행 순서 (실측): EBITDA, EPS, EPS YoY, PER, EV/EBITDA, ROE, 부채비율, 이자보상배율


def _num(v):
    """응답 문자열 → float. 빈값/파싱 실패는 None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _ratio(v):
    """비율류: 응답값/10 이 실제 %."""
    n = _num(v)
    return round(n / 10, 2) if n is not None else None


def fetch_estimate_one(code: str, name: str = ""):
    """한 종목의 추정실적 조회 → 레코드 리스트 (미커버 종목은 빈 리스트).
    네트워크/토큰 실패는 예외로 올려 호출부(run)의 failed 집계 유지."""
    data = kis_auth.kis_get(TR_ID, API_PATH, {"SHT_CD": code}, custtype="P")
    if data is None:
        raise RuntimeError("KIS 응답 없음 (토큰/네트워크)")
    if data.get("rt_cd") != "0":
        log.warning(f"[추정실적] {code} {name} rt_cd={data.get('rt_cd')} {data.get('msg1')}")
        return []

    o1 = data.get("output1") or {}
    if not o1.get("sht_cd"):          # 미커버 종목 (정상 — 커버리지 밖)
        return []

    periods = [x.get("dt", "") for x in (data.get("output4") or [])]
    o2 = data.get("output2") or []
    o3 = data.get("output3") or []
    if len(periods) < 1 or len(o2) < 6:
        log.warning(f"[추정실적] {code} {name} 응답 형태 이상 (periods={len(periods)}, o2={len(o2)})")
        return []

    est_raw = o1.get("estdate", "")
    if len(est_raw) != 8:
        log.warning(f"[추정실적] {code} {name} estdate 이상: {est_raw!r}")
        return []
    est_date = f"{est_raw[:4]}-{est_raw[4:6]}-{est_raw[6:]}"

    def cell(rows, ridx, cidx):
        try:
            return rows[ridx].get(f"data{cidx + 1}")
        except (IndexError, AttributeError):
            return None

    records = []
    for i, dt in enumerate(periods):
        if not dt:
            continue
        records.append({
            "stock_code": code,
            "stock_name": o1.get("item_kor_nm") or name,
            "fiscal_period": dt.rstrip("E"),        # '2026.12E' → '2026.12'
            "is_estimate": dt.endswith("E"),
            "revenue": _num(cell(o2, 0, i)),
            "revenue_yoy": _ratio(cell(o2, 1, i)),
            "op_profit": _num(cell(o2, 2, i)),
            "op_profit_yoy": _ratio(cell(o2, 3, i)),
            "net_profit": _num(cell(o2, 4, i)),
            "net_profit_yoy": _ratio(cell(o2, 5, i)),
            "eps": _ratio(cell(o3, 1, i)),
            "per": _ratio(cell(o3, 3, i)),
            "roe": _ratio(cell(o3, 5, i)),
            "est_date": est_date,
            "opinion": o1.get("rcmd_name") or None,
            "analyst": o1.get("name1") or None,
        })
    return records


def _load_existing(sb):
    """기존 추정치 이력 → {stock_code: {est_date: {fiscal_period: row}}}"""
    rows = fetch_all_pages(
        sb.from_("consensus_estimates")
          .select("stock_code,fiscal_period,is_estimate,revenue,op_profit,est_date")
    )
    by_stock = {}
    for r in rows:
        by_stock.setdefault(r["stock_code"], {}) \
                .setdefault(r["est_date"], {})[r["fiscal_period"]] = r
    return by_stock


def _chg_pct(prev, new):
    """변화율 % (분모=|prev|, 부호로 방향 유지). prev가 없거나 0이면 None."""
    if prev is None or new is None or prev == 0:
        return None
    return round((new - prev) / abs(prev) * 100, 2)


def _detect_revisions(code, name, new_records, existing_by_stock):
    """est_date가 기존 최신보다 새로우면 미래 연도 매출/영업이익 변화폭 기록."""
    hist = existing_by_stock.get(code)
    if not hist or not new_records:
        return []
    new_est = new_records[0]["est_date"]
    prev_dates = [d for d in hist if d < new_est]
    if new_est in hist or not prev_dates:
        return []
    prev = hist[max(prev_dates)]

    revisions = []
    for rec in new_records:
        if not rec["is_estimate"]:
            continue
        p = prev.get(rec["fiscal_period"])
        if not p or not p.get("is_estimate"):
            continue
        rev_chg = _chg_pct(p.get("revenue"), rec["revenue"])
        op_chg = _chg_pct(p.get("op_profit"), rec["op_profit"])
        if rev_chg is None and op_chg is None:
            continue
        revisions.append({
            "stock_code": code,
            "stock_name": rec["stock_name"] or name,
            "fiscal_period": rec["fiscal_period"],
            "prev_est_date": p["est_date"],
            "new_est_date": rec["est_date"],
            "revenue_prev": p.get("revenue"),
            "revenue_new": rec["revenue"],
            "revenue_change_pct": rev_chg,
            "op_profit_prev": p.get("op_profit"),
            "op_profit_new": rec["op_profit"],
            "op_profit_change_pct": op_chg,
        })
    return revisions


def run(codes: dict = None):
    """모니터링 종목 추정실적 수집. codes={name: code} (기본 COMPANY_CODES)."""
    if not kis_auth.get_token():   # 대량 작업 전 fast-fail (호출은 kis_get이 처리)
        log.error("[추정실적] KIS 토큰 발급 실패")
        return 0, 0

    sb = get_supabase_client()
    targets = codes or COMPANY_CODES
    existing = _load_existing(sb)

    all_records, all_revisions, covered, failed = [], [], 0, 0
    for name, code in targets.items():
        try:
            records = fetch_estimate_one(code, name)
        except Exception as e:
            failed += 1
            log.warning(f"[추정실적] {code} {name} 조회 실패: {e}")
            time.sleep(0.5)
            continue
        if records:
            covered += 1
            all_revisions.extend(_detect_revisions(code, name, records, existing))
            all_records.extend(records)
        time.sleep(0.06)

    saved = batch_upsert(sb, "consensus_estimates", all_records,
                         "stock_code,fiscal_period,est_date", chunk=100)
    rev_saved = 0
    if all_revisions:
        rev_saved = batch_upsert(sb, "estimate_revisions", all_revisions,
                                 "stock_code,fiscal_period,new_est_date", chunk=100)

    log.info(f"[추정실적] 완료: 커버 {covered}/{len(targets)}종목, "
             f"스냅샷 {saved}행 upsert, 갱신감지 {rev_saved}건, 조회실패 {failed}")
    return covered, rev_saved


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        code = sys.argv[1]
        run({code: code})
    else:
        run()
