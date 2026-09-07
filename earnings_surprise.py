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

# ── 영업이익 '폭증' 탐지 기준(컨센 무관, 재무 확정치 기반) ──
SURGE_YOY_PCT   = 100.0   # 영업익 YoY 이 %↑ = 폭증(전년동기 흑자 기반일 때만)
SURGE_QOQ_PCT   = 100.0   # 영업익 QoQ 이 %↑ = 폭증(직전분기 흑자 기반일 때만)
MIN_REVENUE_EOK = 50.0    # 매출 하한(억) — 소형주 base effect 노이즈 방지
MIN_OP_EOK      = 30.0    # 영업익 하한(억) — 같은 목적
_MIN_BASE_WON      = 1_000_000_000   # 비교분기(전년/직전) 영업익 10억 하한 — 초저베이스發 허수 %(예: +16000%) 차단
_MIN_CONSENSUS_EOK = 10.0            # 컨센 10억 하한 — 초소형 컨센發 허수 %(예: 컨센 1억→+5500%) 차단


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


# ══════════════════════════════════════════════════════════════════════════════
#  재무 확정치 기반 재조정(reconcile) — 실시간 훅 누락분 회수 + 영업익 폭증 탐지
#  ⚠️ 실시간 잠정공시 훅과 별개로, 저녁 배치가 financials 확정치를 스캔해 적재.
#     컨센 스냅샷이 늦게 잡히거나 실시간 경로가 빠져도 여기서 반드시 회수된다.
# ══════════════════════════════════════════════════════════════════════════════

def _digit_q(quarter) -> int | None:
    """'Q2'/'2'/2 → 2."""
    if quarter is None:
        return None
    s = str(quarter).upper().replace("Q", "").strip()
    return int(s) if s in ("1", "2", "3", "4") else None


def _pick_cfs(existing: dict, r: dict) -> bool:
    """종목별 1행 선택: CFS(연결) 우선, 없으면 OFS(별도)."""
    return existing is None or (r.get("fs_div") == "CFS" and existing.get("fs_div") != "CFS")


def _op_cache(sb, year, quarter) -> dict:
    """{stock_code: {'operating_profit':원, 'fs_div':..}} — 비교분기 영업익 캐시(CFS 우선)."""
    if not year or not quarter:
        return {}
    from db_utils import fetch_all_pages
    rows = fetch_all_pages(
        sb.table("financials")
          .select("stock_code,operating_profit,fs_div")
          .eq("bsns_year", str(year)).eq("quarter", quarter))
    out = {}
    for r in rows or []:
        c = r["stock_code"].split(".")[0]
        if _pick_cfs(out.get(c), r):
            out[c] = r
    return out


def _consensus_map(sb, qkey: str, codes: list) -> dict:
    """{stock_code: op_consensus(억)} — quarterly_consensus 스냅샷 배치 조회."""
    out = {}
    for i in range(0, len(codes), 200):
        batch = codes[i:i + 200]
        try:
            r = (sb.table("quarterly_consensus")
                 .select("stock_code,op_consensus")
                 .eq("quarter", qkey).in_("stock_code", batch).execute())
            for x in (r.data or []):
                if x.get("op_consensus") is not None:
                    out[x["stock_code"]] = float(x["op_consensus"])
        except Exception as e:
            log.debug(f"[재조정] 컨센 배치 조회 실패: {e}")
    return out


def reconcile_quarter(year, quarter, persist: bool = True,
                      monitored_only: bool = True, base_date: str = None) -> list:
    """해당 분기 재무 확정치를 스캔해 어닝 서프라이즈/영업익 폭증을 판정·적재.

    판정 사유(reasons):
      - 'consensus'  : 발표 영업익이 컨센 +{THRESHOLD_PCT}%↑ 상회
      - 'turnaround' : 흑자전환(컨센≤0 또는 직전분기 적자 → 발표 흑자)
      - 'surge'      : 영업익 YoY/QoQ +{SURGE_*}%↑ 폭증(비교분기 흑자 기반)
    소형주 노이즈 방지: 매출≥{MIN_REVENUE_EOK}억·발표OP≥{MIN_OP_EOK}억 게이트(폭증/흑자전환에만).

    persist=False면 계산만(적재 안 함). 반환: 탐지된 rec 리스트(reasons 포함)."""
    from db_utils import fetch_all_pages
    sb = get_supabase_client()
    qd = _digit_q(quarter)
    qkey = _quarter_key(year, qd)
    if not qkey:
        log.warning(f"[재조정] 분기 파싱 실패: {year} {quarter}")
        return []
    bd = base_date or date.today().isoformat()

    codes_filter = None
    if monitored_only:
        comp = fetch_all_pages(sb.table("companies").select("code").eq("is_monitored", True))
        codes_filter = {r["code"].split(".")[0] for r in (comp or [])}

    rows = fetch_all_pages(
        sb.table("financials")
          .select("stock_code,corp_name,operating_profit,revenue,"
                  "op_profit_yoy,op_profit_qoq,fs_div")
          .eq("bsns_year", str(year)).eq("quarter", str(quarter)))
    cur = {}
    for r in rows or []:
        c = r["stock_code"].split(".")[0]
        if codes_filter is not None and c not in codes_filter:
            continue
        if _pick_cfs(cur.get(c), r):
            cur[c] = r
    if not cur:
        log.info(f"[재조정] {year} {quarter} 대상 재무 없음 — 스킵")
        return []

    from format_utils import get_prev_quarter
    py, pq = get_prev_quarter(str(year), str(quarter))
    prevq = _op_cache(sb, py, pq)
    prevy = _op_cache(sb, str(int(year) - 1), str(quarter))
    cons_map = _consensus_map(sb, qkey, list(cur.keys()))

    detected = []
    for code, r in cur.items():
        op_won = r.get("operating_profit")
        if op_won is None:
            continue
        op_eok  = round(op_won / _WON_PER_EOK, 1)
        rev_eok = (r.get("revenue") or 0) / _WON_PER_EOK
        name    = r.get("corp_name") or ""
        op_yoy  = r.get("op_profit_yoy")
        op_qoq  = r.get("op_profit_qoq")
        prev_q_won = (prevq.get(code) or {}).get("operating_profit")
        prev_y_won = (prevy.get(code) or {}).get("operating_profit")

        reasons = set()
        cons = cons_map.get(code)
        surprise_pct = None
        # ── 컨센 대비 ──
        if cons is not None:
            if cons <= 0 and op_eok > 0:
                reasons.add("turnaround")
            elif cons >= _MIN_CONSENSUS_EOK:          # 10억 미만 컨센은 허수 % 방지로 제외
                surprise_pct = round((op_eok - cons) / cons * 100, 1)
                if surprise_pct >= THRESHOLD_PCT:
                    reasons.add("consensus")

        # ── 영업익 폭증 / 흑자전환(재무 확정치 기반) — 규모 게이트 통과분만 ──
        big_enough = op_eok >= MIN_OP_EOK and rev_eok >= MIN_REVENUE_EOK
        if big_enough:
            if prev_q_won is not None and prev_q_won < 0 and op_won > 0:
                reasons.add("turnaround")             # 직전분기 적자→흑자
            if (op_yoy is not None and op_yoy >= SURGE_YOY_PCT
                    and (prev_y_won or 0) >= _MIN_BASE_WON):   # 전년 흑자(≥10억) 기반 YoY 폭증
                reasons.add("surge")
            if (op_qoq is not None and op_qoq >= SURGE_QOQ_PCT
                    and (prev_q_won or 0) >= _MIN_BASE_WON):   # 직전분기 흑자(≥10억) 기반 QoQ 폭증
                reasons.add("surge")

        if not reasons:
            continue

        rec = {
            "stock_code":   code,
            "corp_name":    name,
            "quarter":      qkey,
            "op_actual":    op_eok,
            "op_consensus": round(cons, 1) if cons is not None else None,
            "surprise_pct": surprise_pct if "consensus" in reasons else None,
            # 표시 정합성: base가 10억 미만이면 허수 %이므로 저장/표시하지 않음
            "op_yoy":       round(op_yoy, 1) if (op_yoy is not None and (prev_y_won or 0) >= _MIN_BASE_WON) else None,
            "op_qoq":       round(op_qoq, 1) if (op_qoq is not None and (prev_q_won or 0) >= _MIN_BASE_WON) else None,
            "op_prev":      round(prev_q_won / _WON_PER_EOK, 1) if prev_q_won is not None else None,
            "kind":         "+".join(sorted(reasons)),
            "base_date":    bd,
            "reasons":      reasons,   # 렌더/디버그용(DB엔 미저장)
        }
        detected.append(rec)

    inserted = _persist_reconciled(sb, detected) if (persist and detected) else 0
    log.info(f"[재조정] {year} {quarter} — 탐지 {len(detected)}건 "
             f"(consensus {sum('consensus' in d['reasons'] for d in detected)} · "
             f"surge {sum('surge' in d['reasons'] for d in detected)} · "
             f"turnaround {sum('turnaround' in d['reasons'] for d in detected)})"
             f"{f' → 신규 {inserted}건 적재' if persist else ' [계산만]'}")
    return detected


def _persist_reconciled(sb, detected: list) -> int:
    """탐지 rec을 earnings_surprise에 적재하되 **신규 (종목,분기)만 삽입**한다.
    이미 리스트에 오른 종목은 base_date를 갱신하지 않아(=재발송 안 함) 실적시즌 동안
    매일 밤 같은 종목이 반복 발송되는 것을 막는다(각 종목은 최초 탐지일 1회만 노출)."""
    from collections import defaultdict
    by_q = defaultdict(list)
    for d in detected:
        by_q[d["quarter"]].append(d)
    inserted = 0
    for q, rows in by_q.items():
        codes = [r["stock_code"] for r in rows]
        existing = set()
        for i in range(0, len(codes), 200):
            try:
                r = (sb.table("earnings_surprise").select("stock_code")
                     .eq("quarter", q).in_("stock_code", codes[i:i + 200]).execute())
                existing |= {x["stock_code"] for x in (r.data or [])}
            except Exception as e:
                log.warning(f"[재조정] 기존키 조회 실패 {q}: {e}")
        new_rows = [{k: v for k, v in d.items() if k != "reasons"}
                    for d in rows if d["stock_code"] not in existing]
        if not new_rows:
            continue
        try:
            sb.table("earnings_surprise").upsert(
                new_rows, on_conflict="stock_code,quarter").execute()
            inserted += len(new_rows)
        except Exception as e:
            log.warning(f"[재조정] 적재 실패(테이블/컬럼 확인 — kind/op_yoy/op_qoq/op_prev): {e}")
    return inserted


def _row_reasons(r) -> set:
    """저장 행에서 사유 복원(kind 우선, 없으면 legacy 규칙)."""
    kind = r.get("kind")
    if kind:
        return set(kind.split("+"))
    # legacy(컨센 전용) 행: 흑자전환/컨센상회 규칙으로 복원
    if _is_turnaround(r):
        return {"turnaround"}
    return {"consensus"}


def build_briefing(base_date: str = None) -> str | None:
    """당일 어닝 서프라이즈 리스트 메시지(HTML). 대상 없으면 None."""
    bd = base_date or date.today().isoformat()
    sb = get_supabase_client()
    cols = "corp_name,op_actual,op_consensus,surprise_pct,kind,op_yoy,op_qoq,op_prev"
    try:
        rows = (sb.table("earnings_surprise").select(cols)
                .eq("base_date", bd).execute().data or [])
    except Exception:
        # kind 등 신규 컬럼 미생성(ALTER 前) — 레거시 컬럼으로 폴백
        try:
            rows = (sb.table("earnings_surprise")
                    .select("corp_name,op_actual,op_consensus,surprise_pct")
                    .eq("base_date", bd).execute().data or [])
        except Exception as e:
            log.warning(f"[서프라이즈] 브리핑 조회 실패: {e}")
            return None
    if not rows:
        return None
    return _render_briefing(rows, bd)


def _render_briefing(rows: list, bd: str) -> str | None:
    """탐지/조회된 행 리스트 → HTML 메시지. build_briefing·드라이런 공용."""
    if not rows:
        return None

    _PRIO = {"turnaround": 0, "surge": 1, "consensus": 2}

    def _mag(r, reasons):
        if "surge" in reasons:
            return max(r.get("op_yoy") or 0, r.get("op_qoq") or 0)
        if "consensus" in reasons:
            return r.get("surprise_pct") or 0
        return r.get("op_actual") or 0

    def _line(r) -> str:
        reasons = _row_reasons(r)
        tag = ("🔴" if "turnaround" in reasons else "") \
            + ("🚀" if "surge" in reasons else "") \
            + ("🎯" if "consensus" in reasons else "")
        name = html.escape(r.get("corp_name") or "")
        act  = _fmt_eok(r.get("op_actual"))
        parts = []
        if "consensus" in reasons and r.get("surprise_pct") is not None:
            parts.append(f"컨센 {_fmt_eok(r.get('op_consensus'))} 대비 +{r['surprise_pct']:.1f}%")
        if "surge" in reasons:
            sp = []
            if (r.get("op_yoy") or 0) >= SURGE_YOY_PCT:
                sp.append(f"YoY +{r['op_yoy']:.0f}%")
            if (r.get("op_qoq") or 0) >= SURGE_QOQ_PCT:
                sp.append(f"QoQ +{r['op_qoq']:.0f}%")
            if sp:
                parts.append("영업익 " + "·".join(sp))
        if "turnaround" in reasons:
            prev = r.get("op_prev")
            parts.append(f"흑자전환(직전 {_fmt_eok(prev)})" if prev is not None else "흑자전환")
        return f"{tag} {name} / 발표OP {act} / {' · '.join(parts)}"

    rows.sort(key=lambda r: (min(_PRIO.get(x, 9) for x in _row_reasons(r)),
                             -_mag(r, _row_reasons(r))))
    d = datetime.strptime(bd, "%Y-%m-%d")
    LIMIT = 40
    lines = [
        f"🔴 <b>어닝 서프라이즈 리스트</b> ({d.year}년 {d.month}월 {d.day}일 기준)",
        f"- 영업익 기준: 🎯컨센 +{THRESHOLD_PCT:.0f}%↑ 상회 · "
        f"🚀YoY/QoQ +{SURGE_YOY_PCT:.0f}%↑ 폭증 · 🔴흑자전환",
        "",
    ]
    for r in rows[:LIMIT]:
        lines.append(_line(r))
    if len(rows) > LIMIT:
        lines.append(f"... 외 {len(rows) - LIMIT}개")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build_briefing() or "(당일 대상 없음)")
