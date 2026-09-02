"""
earnings_surprise.py
────────────────────
어닝 서프라이즈 판정 + 메인채널 요약 메시지.

흐름:
  1) DART 잠정실적 공시 도착 → record_from_disclosure() [main.py 훅에서 호출]
     - 공시 원문에서 발표 영업익(실제) 추출 (dart_parser.extract_preliminary_current)
     - 해당 분기 컨센서스 조회 (quarterly_consensus 스냅샷 우선, 없으면 네이버 라이브)
     - surprise = (실제-컨센)/컨센*100, 임계(+10%) 이상이면 earnings_surprise 적재
  2) 장 마감 후 build_briefing() → 당일 서프라이즈 리스트 메시지 → 메인채널 발송

단위: 컨센=네이버 억원, 발표 실제=DART 원 단위 → 억원 변환.
주의(MVP 한계): 잠정실적의 '당기실적'을 단일 분기값으로 가정(financials is_cumulative=False
  추이 표시와 동일 가정). 누적 보고 종목은 오차 가능. 컨센 커버리지 밖(≈32%)·적자 컨센은 제외.
"""

import html
import logging
from datetime import date, datetime

from logger_config import get_logger
from db_client import get_supabase_client

log = get_logger(__name__)

THRESHOLD_PCT = 10.0          # 컨센 대비 이 % 이상 상회 시 리스트 포함
_WON_PER_EOK = 100_000_000    # 1억원 = 1e8 원


def _quarter_key(year, quarter) -> str | None:
    """(2026, 2) → '202606'. 분기→종료월(3/6/9/12)."""
    if not year or quarter not in (1, 2, 3, 4):
        return None
    return f"{int(year)}{quarter * 3:02d}"


def get_consensus_op(code: str, quarter: str) -> float | None:
    """분기 컨센 영업익(억원). quarterly_consensus 스냅샷 우선, 없으면 네이버 라이브 fallback."""
    code = code.split(".")[0]
    # 1) 발표 전 스냅샷 (주 소스)
    try:
        sb = get_supabase_client()
        r = (sb.table("quarterly_consensus")
             .select("op_consensus")
             .eq("stock_code", code).eq("quarter", quarter)
             .limit(1).execute())
        if r.data and r.data[0].get("op_consensus") is not None:
            return float(r.data[0]["op_consensus"])
    except Exception as e:
        log.debug(f"[서프라이즈] 컨센 조회(테이블) 실패 {code} {quarter}: {e}")
    # 2) 네이버 라이브 (아직 컨센=Y인 lag 구간 — 스냅샷 이력 없는 종목 커버)
    try:
        from collect_qtr_consensus import fetch_quarter_consensus
        c = fetch_quarter_consensus(code)
        if c and c.get("quarter") == quarter:
            return c.get("op")
    except Exception as e:
        log.debug(f"[서프라이즈] 컨센 조회(네이버) 실패 {code} {quarter}: {e}")
    return None


def compute_surprise(code: str, rcept_no: str) -> dict | None:
    """DART 잠정실적 원문 → 발표 영업익 추출 + 컨센 조회 → 서프라이즈 계산(저장 안 함).
    반환 {'quarter','op_actual','op_consensus','surprise_pct'} 또는
    None(추출 실패 / 컨센 없음 / 적자·0 컨센)."""
    try:
        from dart_doc import _fetch_html, _build_kv
        from dart_parser import extract_preliminary_current
        html_doc = _fetch_html(rcept_no)
        cur = extract_preliminary_current(_build_kv(html_doc)) if html_doc else None
    except Exception:
        log.debug(f"[서프라이즈] 원문 추출 실패: {rcept_no}")
        return None
    if not cur:
        return None
    op_won = cur.get("operating_profit")
    quarter = _quarter_key(cur.get("year"), cur.get("quarter"))
    if op_won is None or not quarter:
        return None
    op_actual = round(op_won / _WON_PER_EOK, 1)   # 억원
    cons = get_consensus_op(code, quarter)
    if cons is None:
        return None
    if cons <= 0:
        # 적자·보합 컨센 → %계산 불가. 발표가 흑자면 '흑자전환'(최대 서프라이즈)로 포함,
        # 적자 발표면 판정 제외.
        if op_actual > 0:
            return {"quarter": quarter, "op_actual": op_actual,
                    "op_consensus": round(cons, 1), "surprise_pct": None,
                    "turnaround": True}
        return None
    return {
        "quarter": quarter,
        "op_actual": op_actual,
        "op_consensus": round(cons, 1),
        "surprise_pct": round((op_actual - cons) / cons * 100, 1),
        "turnaround": False,
    }


def record_if_surprise(code: str, corp_name: str, sp: dict) -> dict | None:
    """서프라이즈면 earnings_surprise 저장: 컨센 +10%↑ 상회 또는 흑자전환."""
    if not sp:
        return None
    pct = sp.get("surprise_pct")
    if not sp.get("turnaround") and (pct is None or pct < THRESHOLD_PCT):
        return None
    rec = {
        "stock_code": code.split(".")[0],
        "corp_name": corp_name,
        "quarter": sp["quarter"],
        "op_actual": sp["op_actual"],
        "op_consensus": sp["op_consensus"],
        "surprise_pct": pct,   # 흑자전환은 None(NULL) 저장 — 렌더 시 op_consensus≤0으로 판별
        "base_date": date.today().isoformat(),
    }
    try:
        sb = get_supabase_client()
        sb.table("earnings_surprise").upsert(
            rec, on_conflict="stock_code,quarter").execute()
        _tag = "흑자전환" if sp.get("turnaround") else f"+{pct}%"
        log.info(f"[서프라이즈] {corp_name}({code}) {sp['quarter']} "
                 f"실제 {sp['op_actual']}억 vs 컨센 {sp['op_consensus']}억 = {_tag}")
    except Exception as e:
        log.warning(f"[서프라이즈] 저장 실패 (earnings_surprise 테이블 미생성?) {code}: {e}")
        return None
    return rec


def record_from_disclosure(code: str, corp_name: str, rcept_no: str) -> dict | None:
    """계산+저장 일괄(편의). 개별 단계는 compute_surprise · record_if_surprise."""
    return record_if_surprise(code, corp_name, compute_surprise(code, rcept_no))


def consensus_line(sp: dict) -> str:
    """잠정실적 공시 메시지에 붙일 '컨센 대비' 한 줄. sp=compute_surprise 결과.
    컨센 없으면 빈 문자열. ⚠️ main._build_msg가 detail 전체를 html.escape 하므로
    여기서는 HTML 태그(<b> 등) 금지 — plain text만(강조는 이모지로)."""
    if not sp:
        return ""
    cons = _fmt_eok(sp["op_consensus"])
    if sp.get("turnaround"):
        return f"🔴 어닝 서프라이즈 — 적자 예상({cons}) 뒤집고 흑자전환 (발표 {_fmt_eok(sp['op_actual'])})"
    pct = sp["surprise_pct"]
    if pct >= THRESHOLD_PCT:
        return f"🔴 어닝 서프라이즈 — 영업익 컨센 +{pct:.1f}% 상회 (예상 {cons})"
    if pct > 0:
        return f"🎯 컨센 상회 — 영업익 예상 대비 +{pct:.1f}% (예상 {cons})"
    if pct < 0:
        return f"🔵 컨센 하회 — 영업익 예상 대비 {pct:.1f}% (예상 {cons})"
    return f"➖ 컨센 부합 — 영업익 예상 수준 (예상 {cons})"


def _fmt_eok(v) -> str:
    """억원 float → '629억' / '1.0조' 표시."""
    if v is None:
        return "-"
    neg = v < 0
    a = abs(v)
    s = f"{a / 10000:.1f}조" if a >= 10000 else f"{a:,.0f}억"
    return ("-" if neg else "") + s


def _is_turnaround(r) -> bool:
    """저장된 행이 흑자전환인지 판별: 적자·보합 컨센(≤0) + 흑자 발표(>0)."""
    c = r.get("op_consensus")
    return c is not None and c <= 0 and (r.get("op_actual") or 0) > 0


def build_briefing(base_date: str = None) -> str | None:
    """당일 어닝 서프라이즈 리스트 메시지(HTML). 대상 없으면 None."""
    bd = base_date or date.today().isoformat()
    try:
        sb = get_supabase_client()
        rows = (sb.table("earnings_surprise")
                .select("corp_name,op_actual,op_consensus,surprise_pct")
                .eq("base_date", bd)
                .execute().data or [])
    except Exception as e:
        log.warning(f"[서프라이즈] 브리핑 조회 실패: {e}")
        return None
    if not rows:
        return None

    # 흑자전환 최상단, 그다음 상회율 내림차순 (흑자전환은 surprise_pct=NULL)
    rows.sort(key=lambda r: (0 if _is_turnaround(r) else 1,
                             -(r.get("surprise_pct") or 0)))
    d = datetime.strptime(bd, "%Y-%m-%d")
    lines = [
        f"🔴 <b>어닝 서프라이즈 리스트</b> ({d.year}년 {d.month}월 {d.day}일 기준)",
        f"- 영업익 기준 선정 (컨센 +{THRESHOLD_PCT:.0f}% 이상 상회 · 흑자전환 포함)",
        "",
        "(종목명 / 발표OP / 예상OP / 예상대비)",
    ]
    for r in rows:
        name = html.escape(r.get("corp_name") or "")
        act = _fmt_eok(r.get("op_actual"))
        cons = _fmt_eok(r.get("op_consensus"))
        if _is_turnaround(r):
            lines.append(f"{name} / {act} / {cons} (흑자전환)")
        else:
            lines.append(f"{name} / {act} / {cons} (+{r.get('surprise_pct'):.1f}%)")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build_briefing() or "(당일 대상 없음)")
