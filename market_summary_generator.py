"""
market_summary_generator.py
────────────────────────────
투자포인트 요약 생성기 — 시장 데이터를 분석해 market_investment_summary 테이블에 저장.

사용법:
    python market_summary_generator.py             # 오늘 날짜 기준 생성
    python market_summary_generator.py 2025-05-28  # 특정 날짜 지정

실행 시점:
    - 장 마감 후 (16:10 이후) 자동 실행 권장
    - bati_bot 스케줄러 또는 cron에서 호출

의존:
    - supabase-py : pip install supabase
    - .env 또는 환경변수: SUPABASE_URL, SUPABASE_KEY
"""

import sys
import json
import logging
from logger_config import get_logger
try:
    from db_utils import fetch_all_pages  # PostgREST 1000행 한도 회피
except Exception:
    fetch_all_pages = None
log = get_logger(__name__)

from datetime import datetime, timedelta
from typing import Optional

# ── Supabase 클라이언트 ──────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
try:
    from db_client import get_supabase_client
    sb = get_supabase_client()
except Exception as e:
    sb = None
    logging.warning(f"[SummaryGen] Supabase 연결 실패: {e}")



# ── 임계값 ────────────────────────────────────────────────────────────────────
THR_STRONG   =  0.5
THR_WEAK     = -0.5
THR_VIX_HIGH =  25
THR_VIX_FEAR =  20
THR_US10Y    =  4.5
THR_S5       =  1.5   # 5일 누적 강세/약세 임계 — 프론트 market-insight.js S5와 동일
THR_TURN     =  1.0   # 직전 해외 세션이 이만큼 이상 같은 방향이면 '엇갈림' → '전환 관찰'로 완화
THR_SURGE    =  5.0   # 당일 업종 급등 임계 — 되돌림 주의(과열)를 붙일 최소 당일 상승폭

# ⚠️ 아래 두 상수는 DB 조회 실패 시 폴백 — 실제 값은 run() 시작 시
# us_etf_map 테이블(단일 출처, 프론트 loadUskrMap과 동일)에서 갱신된다.
KR_INDUSTRIES = ["반도체","바이오","테크","로봇","2차전지","조선","뷰티","엔터","신재생","소비재","우주"]

USKR_MAP = {
    "반도체":  ["SOXX","SMH","SOXL"],
    "바이오":  ["IBB","XBI","LABU"],
    "로봇":    ["BOTZ","ROBO","IRBO"],
    "우주":    ["ARKX","UFO"],
    "2차전지": ["LIT","BATT","DRIV"],
    "소비재":  ["XLY","ONLN"],
    "엔터":    ["XLC","PEJ"],
    "조선":    ["BOAT","SEA"],
    "테크":    ["VGT","XLK","QQQ","ARKK"],
    "뷰티":    ["RTH"],
    "신재생":  ["ICLN","QCLN","TAN"],
}


def refresh_industry_config():
    """us_etf_map 테이블에서 USKR_MAP·KR_INDUSTRIES 갱신 — 하드코딩 드리프트 방지.

    프론트(chart-uskr.js loadUskrMap)·collect_us_etf.py와 동일하게 테이블을
    단일 출처로 사용. 조회 실패 시 모듈 폴백 상수 유지.
    """
    global USKR_MAP, KR_INDUSTRIES
    if not sb:
        return
    try:
        rows = sb.table("us_etf_map").select("industry,ticker").execute().data or []
        m: dict = {}
        for r in rows:
            m.setdefault(r["industry"], [])
            if r["ticker"] not in m[r["industry"]]:
                m[r["industry"]].append(r["ticker"])
        if m:
            USKR_MAP = m
            KR_INDUSTRIES = list(m.keys())
            log.info(f"[refresh_industry_config] us_etf_map 로드: {len(m)}개 산업")
    except Exception as e:
        log.warning(f"[refresh_industry_config] 조회 실패 — 폴백 상수 사용: {e}")


# ════════════════════════════════════════════════════════════
# 데이터 조회
# ════════════════════════════════════════════════════════════

def fetch_macro(target_date: str) -> dict:
    """macro_data 테이블에서 최신 1건 조회.
    금리·환율은 '수준'이 아니라 '변화'일 때만 체크포인트에 올리므로 직전 행도 함께 담는다
    (_prev 키). 실측상 환율 1450원↑은 31일 연속, 금리 4.5%↑는 15거래일 연속이라
    수준 기준으로는 매일 같은 문장이 나갔다."""
    if not sb:
        return {}
    try:
        res = sb.table("macro_data").select("*") \
            .lte("base_date", target_date) \
            .order("base_date", desc=True).limit(2).execute()
        rows = res.data or [{}]
        cur = rows[0]
        cur["_prev"] = rows[1] if len(rows) > 1 else {}
        return cur
    except Exception as e:
        log.warning(f"[fetch_macro] {e}")
        return {}


def fetch_market_summary(target_date: str) -> list:
    """
    market_data 테이블에서 target_date 이하 최신 거래일 전체 조회.
    (eq 고정이면 당일 행이 없는 날 신고가·외국인·거래대금 섹션이 통째로 빔)
    필드: stock_code, corp_name, price_change_rate, market_cap,
          foreign_net_buy, volume, market, hgpr_cls_code
    """
    if not sb:
        return []
    try:
        latest = sb.table("market_data").select("base_date")             .lte("base_date", target_date)             .order("base_date", desc=True).limit(1).execute()
        if not latest.data:
            return []
        base = latest.data[0]["base_date"]
        if base != target_date:
            log.info(f"[fetch_market_summary] {target_date} 데이터 없음 → {base} 사용")
        qb = sb.table("market_data")             .select("stock_code,corp_name,price_change_rate,market_cap,foreign_net_buy,volume,price,market,hgpr_cls_code")             .eq("base_date", base)
        # PostgREST 1000행 한도 -> 전체 상장종목(~2,600개/일) 페이지네이션
        return fetch_all_pages(qb) if fetch_all_pages else (qb.execute().data or [])
    except Exception as e:
        log.warning(f"[fetch_market_summary] {e}")
        return []


def fetch_us_etf(target_date: str, kr_days: list = None) -> dict:
    """
    us_market 테이블에서 해외 ETF 산업 평균 등락률 조회.
    반환: { industry: { d1: 최신 세션 평균, d5: 창 전체 복리 누적 } }
    d5 = 일별 등가중 평균의 복리 누적 — 프론트 buildInsightData(market-insight.js)와 동일 기준.

    kr_days를 주면 **시차 정렬**한다: 한국 D일 장은 직전 해외 세션에 반응하므로,
    해외 창을 [한국 창 첫날 직전 세션 ~ 한국 창 마지막날 직전 세션]으로 맞춘다.
    (연휴로 한국이 쉬는 동안의 해외 세션도 포함 — 한 한국 세션에 해외 2개가 매핑될 수 있다)
    구: 양쪽 다 "최근 5거래일"을 그냥 겹쳐 비교 → 한국 창에만 당일이 들어가고 해외는
        한 세션 뒤처진 채 비교돼, 같은 정보 구간이 아닌 것을 대조했다.
    """
    if not sb:
        return {}
    try:
        res = sb.table("us_market")             .select("base_date,industry,ticker,chg_pct")             .lte("base_date", target_date)             .order("base_date", desc=True).limit(600).execute()
        rows = res.data or []
        if not rows:
            return {}
        all_days = sorted({r["base_date"] for r in rows})
        if kr_days:
            prior = [d for d in all_days if d < kr_days[0]]
            start = prior[-1] if prior else all_days[0]
            last5 = [d for d in all_days if start <= d < kr_days[-1]] or all_days[-5:]
        else:
            last5 = all_days[-5:]
        result = {}
        for ind in KR_INDUSTRIES:
            tickers = USKR_MAP.get(ind, [])
            day_vals = {}
            for r in rows:
                if r.get("industry") == ind and r.get("ticker") in tickers                         and r.get("chg_pct") is not None and r["base_date"] in last5:
                    day_vals.setdefault(r["base_date"], []).append(r["chg_pct"])
            if not day_vals:
                continue
            ds = sorted(day_vals.keys())
            d1 = sum(day_vals[ds[-1]]) / len(day_vals[ds[-1]])
            cum = 100.0
            for dt in ds:
                avg = sum(day_vals[dt]) / len(day_vals[dt])
                cum *= (1 + avg / 100)
            result[ind] = {"d1": round(d1, 2), "d5": round(cum - 100, 2)}
        return result
    except Exception as e:
        log.warning(f"[fetch_us_etf] {e}")
        return {}


def fetch_disclosures(target_date: str) -> list:
    """daily_disclosures 테이블에서 해당 날짜 공시 조회."""
    if not sb:
        return []
    try:
        res = sb.table("daily_disclosures") \
            .select("corp_name,report_nm,category,market_cap") \
            .eq("base_date", target_date).execute()
        return res.data or []
    except Exception as e:
        log.warning(f"[fetch_disclosures] {e}")
        return []


def fetch_industry_trend(target_date: str, days: int = 5) -> dict:
    """
    market_data에서 최근 N일간 산업별 평균 등락률 계산.
    산업 매핑은 모니터링 종목(companies.is_monitored=True) 전체 기준 —
    프론트(getIndustryMap -> market-overview 집계)와 동일 유니버스.
    (구버전은 전체 상장 codes[:500] 절단으로 임의 부분집합 평균이 됐음)
    반환: { industry: { d1: float, d5: float } }
    d5 = 일별 등가중 평균의 복리 누적 — 프론트 indCumReturn(config.js)과 동일 기준.
    """
    if not sb:
        return {}
    try:
        # 회사별 산업 조회 — 모니터링 종목 전체(~312개, 단일 조회로 절단 없음)
        ind_res = sb.table("companies")             .select("code,industry")             .eq("is_monitored", True).execute()
        # companies.code는 .KS/.KQ suffix 포함 가능 — market_data.stock_code(clean)와 매칭되도록 제거
        ind_map = {r["code"].split(".")[0]: r["industry"]
                   for r in (ind_res.data or []) if r.get("industry") and r.get("code")}
        if not ind_map:
            return {}

        from_date = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=days+3)).strftime("%Y-%m-%d")
        qb = sb.table("market_data")             .select("base_date,stock_code,price_change_rate")             .gte("base_date", from_date)             .lte("base_date", target_date)             .in_("stock_code", list(ind_map.keys()))             .not_.is_("price_change_rate", "null")
        # PostgREST 1000행 한도 -> 다일 x 모니터링 종목 페이지네이션
        rows = fetch_all_pages(qb) if fetch_all_pages else (qb.execute().data or [])
        if not rows:
            return {}

        # 날짜별 산업 평균 등락률 계산
        from collections import defaultdict
        day_ind_chg: dict = defaultdict(lambda: defaultdict(list))
        for r in rows:
            ind = ind_map.get(r["stock_code"])
            if ind:
                day_ind_chg[r["base_date"]][ind].append(r["price_change_rate"])

        # 최근 5거래일 (오래된 -> 최신) — 복리 누적 순서 보장
        last5 = sorted(day_ind_chg.keys())[-5:]
        result = {"_days": last5}   # 해외 창 시차 정렬용 (fetch_us_etf가 참조)
        for ind in KR_INDUSTRIES:
            d1 = None
            cum = 100.0
            cum_x = 100.0     # 최신일 제외 누적 — 5일 성과가 하루에서 나왔는지 판별용
            has_any = False
            for dt in last5:
                vals = day_ind_chg[dt].get(ind, [])
                if not vals:
                    continue
                avg = sum(vals) / len(vals)
                cum *= (1 + avg / 100)
                has_any = True
                if dt == last5[-1]:
                    d1 = avg
                else:
                    cum_x *= (1 + avg / 100)
            d5 = round(cum - 100, 2) if has_any else None
            result[ind] = {"d1": d1, "d5": d5,
                           "d5x": round(cum_x - 100, 2) if has_any else None}
        return result
    except Exception as e:
        log.warning(f"[fetch_industry_trend] {e}")
        return {}


# ════════════════════════════════════════════════════════════
# 분석 로직
# ════════════════════════════════════════════════════════════

def _josa(word: str, with_batchim: str, without: str) -> str:
    """받침 유무로 조사 선택 — '로봇이/반도체가', '로봇은/반도체는'."""
    if not word:
        return without
    ch = word[-1]
    if '가' <= ch <= '힣':
        return with_batchim if (ord(ch) - 0xAC00) % 28 else without
    return without


def _fmt(v) -> str:
    if v is None:
        return "—"
    return f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"


def analyze(macro: dict, market_rows: list, us_etf: dict, ind_trend: dict, discs: list) -> dict:
    """모든 데이터를 통합해 5섹션 요약 dict 반환."""

    kospi_chg   = macro.get("kospi_chg")  or 0
    kosdaq_chg  = macro.get("kosdaq_chg") or 0
    sp500_chg   = macro.get("sp500_chg")  or 0
    nasdaq_chg  = macro.get("nasdaq_chg") or 0
    sp500_fut_chg = macro.get("sp500_fut_chg")
    vix         = macro.get("vix")        or 0
    us10y       = macro.get("us10y")      or 0
    usd_krw     = macro.get("usd_krw")

    # ── 장세 판단 ──
    kr_avg = (kospi_chg + kosdaq_chg) / 2
    if kr_avg >= 1.0:        kr_mood, kr_label = "strong",    "강세"
    elif kr_avg >= 0.3:      kr_mood, kr_label = "mild-up",   "소폭 상승"
    elif kr_avg <= -1.0:     kr_mood, kr_label = "weak",      "약세"
    elif kr_avg <= -0.3:     kr_mood, kr_label = "mild-down", "소폭 하락"
    else:                    kr_mood, kr_label = "flat",      "보합"

    defense_inds = ["뷰티","소비재"]
    growth_inds  = ["반도체","바이오","테크","로봇","우주"]
    defense_avg  = sum((us_etf.get(i) or {}).get("d1") or 0 for i in defense_inds) / len(defense_inds)
    growth_avg   = sum((us_etf.get(i) or {}).get("d1") or 0 for i in growth_inds) / len(growth_inds)

    if vix >= THR_VIX_HIGH:              market_regime = "risk-off"
    elif defense_avg > growth_avg + 0.5: market_regime = "방어주 장세"
    elif growth_avg > defense_avg + 0.5: market_regime = "성장주 장세"
    elif (sp500_chg + nasdaq_chg) / 2 >= THR_STRONG: market_regime = "risk-on"
    elif (sp500_chg + nasdaq_chg) / 2 <= THR_WEAK:   market_regime = "관망"
    else:                                market_regime = "혼조"

    # ── 산업 강약 정렬 ──
    # 주도 업종(한줄요약)은 당일 d1, 강세/약세 업종은 5일 누적 d5 — 프론트 _buildLiveSections와 동일
    kr_sorted = sorted(
        [(ind, ind_trend[ind]["d1"]) for ind in KR_INDUSTRIES if ind in ind_trend and ind_trend[ind]["d1"] is not None],
        key=lambda x: x[1], reverse=True
    )
    kr_sorted5 = sorted(
        [(ind, ind_trend[ind]["d5"]) for ind in KR_INDUSTRIES if ind in ind_trend and ind_trend[ind]["d5"] is not None],
        key=lambda x: x[1], reverse=True
    )
    strong_inds = [ind for ind, chg in kr_sorted5 if chg > 0][:4]
    weak_inds   = [ind for ind, chg in reversed(kr_sorted5) if chg < 0][:4]

    # ── 52주 신고가 종목 ──
    HGPR_VALS = {"신고가", "52주 신고가", "1"}
    hgpr_rows = [r for r in market_rows if r.get("hgpr_cls_code") in HGPR_VALS]
    hgpr_rows.sort(key=lambda r: r.get("market_cap") or 0, reverse=True)
    hgpr_count = len(hgpr_rows)
    hgpr_names = [r["corp_name"] for r in hgpr_rows[:3] if r.get("corp_name")]

    # ── 외국인 순매수 상위 ──
    frgn_rows = sorted(
        [r for r in market_rows if (r.get("foreign_net_buy") or 0) > 0 and r.get("corp_name")],
        key=lambda r: r.get("foreign_net_buy") or 0, reverse=True
    )
    top_frgn = [r["corp_name"] for r in frgn_rows[:3]]

    # ── 거래대금 상위 (volume × price 근사) ──
    tv_rows = sorted(
        [r for r in market_rows if r.get("volume") and r.get("price") and r.get("corp_name")],
        key=lambda r: (r["volume"] or 0) * (r["price"] or 0), reverse=True
    )
    top_tv = [r["corp_name"] for r in tv_rows[:5]]

    # ── 해외 크로스 신호 — 5일 누적 기준(단일일 노이즈 제거, 프론트 S5와 동일) ──
    # 창은 fetch_us_etf에서 시차 정렬됨(한국 D ↔ 직전 해외 세션).
    # 최근 방향 가드: 5일은 엇갈려도 **한국이 이미 반응한 직전 해외 세션**이 같은 방향이면
    # '엇갈림'이 아니라 따라잡기 초입일 수 있다 → turn_watch로 분리해 단정을 피한다.
    sync_up, lag_inds, decouple_risk, sync_down, turn_watch = [], [], [], [], []
    kr_only_down, kr_only_up = [], []
    for ind in KR_INDUSTRIES:
        us5 = (us_etf.get(ind) or {}).get("d5")
        kr5 = ind_trend.get(ind, {}).get("d5")
        if us5 is None or kr5 is None:
            continue
        us1 = (us_etf.get(ind) or {}).get("d1")
        kr1 = ind_trend.get(ind, {}).get("d1")
        kr5x = ind_trend.get(ind, {}).get("d5x")
        sig = {"ind": ind, "us": us5, "kr": kr5, "us1": us1, "kr1": kr1, "kr5x": kr5x}
        if us5 >= THR_S5 and kr5 <= -THR_S5:
            if us1 is not None and kr1 is not None and us1 <= -THR_TURN and kr1 < 0:
                turn_watch.append(dict(sig, dir="down"))   # 해외도 직전 세션 하락 → 후행 기대 약화
            else:
                lag_inds.append(sig)
        elif us5 <= -THR_S5 and kr5 >= THR_S5:
            if us1 is not None and kr1 is not None and us1 >= THR_TURN and kr1 > 0:
                turn_watch.append(dict(sig, dir="up"))     # 해외도 직전 세션 반등 → 따라잡기 가능성
            else:
                decouple_risk.append(sig)
        elif us5 >= THR_S5 and kr5 >= THR_S5:
            sync_up.append(sig)
        elif us5 <= -THR_S5 and kr5 <= -THR_S5:
            sync_down.append(sig)
        # 해외는 보합권인데 한국만 움직인 구간 — 시차 정렬 후 '동반'에서 빠지는 부분.
        # 업종별로 나열하면 소음이라 아래에서 시장 단위로 묶어 한 줄로 보고한다.
        elif kr5 <= -THR_S5:
            kr_only_down.append(sig)
        elif kr5 >= THR_S5:
            kr_only_up.append(sig)

    # 디커플·후행 신호는 '심각도'(한국-해외 5일 갭)순으로 정렬 — 헤드라인/전략이
    # 목록 선언순(임의)이 아니라 가장 극단적인 업종을 가리키게 한다.
    # 예: 신재생(한국 +9.2 vs 해외 -3.4)이 로봇(+4.1 vs -2.3)보다 앞선다.
    decouple_risk.sort(key=lambda s: (s["kr"] or 0) - (s["us"] or 0), reverse=True)
    lag_inds.sort(key=lambda s: (s["us"] or 0) - (s["kr"] or 0), reverse=True)
    # 되돌림 근거 = 주도 업종의 당일 급등(kr1=d1 ≥ THR_SURGE)이 최근 상승분 대부분 —
    # 직전 4일(d5x) 0 이하. '해외만 하락'(디커플)은 되돌림 근거로 보지 않는다(사용자 결정).
    _lead_ind = kr_sorted[0][0] if kr_sorted else None
    _lead_chg = kr_sorted[0][1] if kr_sorted else None
    _lead_d5x = (ind_trend.get(_lead_ind, {}) or {}).get("d5x") if _lead_ind else None
    lead_overext = (_lead_chg is not None and _lead_chg >= THR_SURGE
                    and _lead_d5x is not None and _lead_d5x <= 0 and kr_avg > -2.0)

    def _overext(sig) -> bool:
        """디커플 업종의 당일 급등 집중 여부 — risk/watch에서 '되돌림' 단정 게이트."""
        k1, k5x = sig.get("kr1"), sig.get("kr5x")
        return k1 is not None and k5x is not None and k1 >= THR_SURGE and k5x <= 0


    # ────────────────────────────────────────────────────────
    # 5섹션 빌드
    # ────────────────────────────────────────────────────────

    # ① 핵심 흐름
    flow_summary = {
        "market_mood":       kr_mood,
        "market_mood_label": kr_label,
        "market_regime":     market_regime,
        "kospi_chg":         kospi_chg,
        "kosdaq_chg":        kosdaq_chg,
        "sp500_chg":         sp500_chg,
        "nasdaq_chg":        nasdaq_chg,
        "vix":               vix,
        "us10y":             us10y,
        "usd_krw":           usd_krw,
        "strong_industries": strong_inds,
        "weak_industries":   weak_inds,
        "top_frgn_buy":      top_frgn,
        "top_trading_value": top_tv,
    }

    # ② 주목할 투자포인트
    # 순서 중요: 프론트 DB 경로(_loadSummaryFromDB)는 key_points[0]만 '기회' 배지로
    # 표시한다 → 프론트 라이브 로직과 동일하게 후행 선점 → 동반 강세 → … 우선순위.
    # 레짐 게이트: 방어 국면(당일 급락·VIX 공포)에선 '선점/진입' 대신 관망 조건부 —
    # 환경(온도계)은 "지켜라", 전략은 "들어가라"가 공존하는 모순 방지 (프론트 게이트와 동일 기준).
    defensive = kr_avg <= -2.0 or vix >= THR_VIX_HIGH
    key_points = []
    if lag_inds:
        x = lag_inds[0]
        key_points.append(
            f"{x['ind']}: 해외가 먼저 올랐습니다 — 최근 5일 해외 {_fmt(x['us'])}, 한국 {_fmt(x['kr'])}. "
            + ("급락이 진정되는지 먼저 확인할 구간입니다" if defensive
               else "국내 수급이 따라붙는지 확인할 구간입니다"))
    if len(sync_up) >= 2:
        inds = " · ".join(x["ind"] for x in sync_up[:3])
        key_points.append(f"{inds}: 해외와 한국이 같이 오르고 있습니다 (최근 5일 동반 상승)")
    if hgpr_count >= 3:
        nm = " · ".join(hgpr_names[:2])
        key_points.append(f"52주 신고가 {hgpr_count}개 — {nm}{'등' if hgpr_count > 2 else ''}")
    if top_frgn and kospi_chg > 0:
        key_points.append(f"외국인 순매수 집중: {' · '.join(top_frgn[:2])}")
    if top_tv:
        key_points.append(f"거래대금 상위: {' · '.join(top_tv[:3])}")
    if len(kr_only_up) >= 3:
        inds = " · ".join(x["ind"] for x in kr_only_up[:3])
        key_points.append(
            f"🇰🇷 한국이 해외보다 강합니다 — {len(kr_only_up)}개 업종에서 해외는 보합권인데 "
            f"한국만 최근 5일 상승 ({inds} 등)")
    if not key_points:
        key_points.append("뚜렷한 기회 신호 없음")

    # ③ 리스크 요인
    # 순서 중요: 프론트 DB 경로는 risk_factors[0]만 '리스크' 배지로 표시 →
    # 시장 급락(그 자체가 1순위 리스크)을 크로스 신호·매크로 레벨보다 앞에.
    risk_factors = []
    if kr_avg <= -2.0:
        risk_factors.append(f"🚨 코스피/닥 당일 평균 {_fmt(kr_avg)} 급락 — 후속 하락·반대매매 주의, 성급한 저가 매수 금지")
    if vix >= THR_VIX_HIGH:
        risk_factors.append(f"⚠️ VIX {vix:.0f} 공포 구간 — 변동성 확대 주의")
    elif vix >= THR_VIX_FEAR:
        risk_factors.append(f"⚠️ VIX {vix:.0f} 주의 구간")
    if decouple_risk:
        r = decouple_risk[0]
        # 되돌림은 '당일 급등 집중'(과열)일 때만 리스크로 단정 — 단순 디커플은 사실만 전달.
        if _overext(r):
            risk_factors.append(
                f"⚡ {r['ind']}{_josa(r['ind'], '은', '는')} 5일 상승분 대부분이 최근 하루"
                f"({_fmt(r['kr1'])})에서 나왔습니다 (직전 4일 {_fmt(r['kr5x'])}) — 되돌림 위험을 함께 보세요")
        else:
            risk_factors.append(
                f"ℹ️ {r['ind']}{_josa(r['ind'], '은', '는')} 해외와 방향이 갈립니다 — "
                f"최근 5일 한국 {_fmt(r['kr'])}, 해외 {_fmt(r['us'])} (되돌림 단정 아님, 지속 여부 관찰)")
    if sync_down:
        inds = " · ".join(x["ind"] for x in sync_down[:3])
        risk_factors.append(f"🔵 {inds}: 해외와 한국이 같이 밀리고 있습니다 (최근 5일 동반 하락)")
    if len(kr_only_down) >= 3:
        inds = " · ".join(x["ind"] for x in kr_only_down[:3])
        risk_factors.append(
            f"🇰🇷 한국이 해외보다 부진합니다 — {len(kr_only_down)}개 업종에서 해외는 보합권인데 "
            f"한국만 최근 5일 하락 ({inds} 등)")
    if us10y >= THR_US10Y:
        risk_factors.append(f"📊 미 10년 금리 {us10y:.3f}% — 고금리 부담")
    if usd_krw and usd_krw >= 1450:
        risk_factors.append(f"💱 환율 {usd_krw:,.0f}원 — 고환율 부담")
    if not risk_factors:
        risk_factors.append("✅ 현재 주요 리스크 신호 없음")

    # ④ 내일 체크포인트 (마감 브리핑에 나가는 섹션)
    # 마감 시점엔 '지금 팔아라'가 아니라 '내일 뭘 볼까'가 유효하다.
    # 오늘로 끝나는 회고성 항목(공시 건수)과 매일 같은 매크로 수준은 넣지 않는다.
    watch_events = []
    # 내일 아침까지 영향이 이어지는 리스크는 맨 앞
    if kr_avg <= -2.0:
        watch_events.append("🚨 오늘 급락 — 내일 반대매매 물량·후속 하락 여부부터 확인")
    if vix >= THR_VIX_HIGH:
        watch_events.append(f"🚨 VIX {vix:.0f} 공포 구간 — 변동성이 이어지는지 확인")
    # 당일 급등 집중(과열)인 디커플만 내일 체크포인트로 이어간다 — 헤드라인 되돌림 주의와
    # 정합. 단순 디커플(해외만 하락)은 되돌림 근거가 아니라 넣지 않는다.
    if decouple_risk and _overext(decouple_risk[0]):
        _d = decouple_risk[0]
        watch_events.append(
            f"🔄 {_d['ind']}: 오늘 급등분({_fmt(_d['kr1'])})이 유지되는지 확인 "
            f"(직전 4일 {_fmt(_d['kr5x'])})")
    # 5일은 엇갈렸지만 직전 해외 세션이 같은 방향 — 단정 대신 "하루 더 확인"
    for t in turn_watch[:2]:
        if t["dir"] == "up":
            watch_events.append(
                f"🔄 {t['ind']}: 최근 5일은 한국 {_fmt(t['kr'])} / 해외 {_fmt(t['us'])}로 엇갈렸지만, "
                f"직전 해외 세션이 {_fmt(t['us1'])}였습니다 — 따라잡기인지 하루 더 확인")
        else:
            watch_events.append(
                f"🔄 {t['ind']}: 해외 5일 {_fmt(t['us'])}이지만 직전 세션은 {_fmt(t['us1'])}였습니다 — "
                f"국내가 따라 오를 근거가 약해졌는지 확인")
    # 보합(±0.3% 이내)은 체크포인트가 아니다 — 방향이 있을 때만
    if sp500_fut_chg is not None and abs(sp500_fut_chg) >= 0.3:
        dir_str = "상승" if sp500_fut_chg > 0 else "하락"
        watch_events.append(f"🇺🇸 오늘 밤 S&P500 선물 {_fmt(sp500_fut_chg)} {dir_str} — 내일 갭 방향 참고")
    # 매크로는 '수준'이 아니라 '변화'일 때만 — 매일 같은 값은 배경이지 체크포인트가 아니다
    _prev = macro.get("_prev") or {}
    _p10y = _prev.get("us10y")
    if us10y and _p10y and abs(us10y - _p10y) >= 0.10:
        watch_events.append(
            f"📊 미 10년 금리 {us10y:.3f}% ({'+' if us10y > _p10y else ''}{us10y - _p10y:.3f}%p) — 하루 새 큰 폭 변동")
    _pfx = _prev.get("usd_krw")
    if usd_krw and _pfx and abs(usd_krw - _pfx) >= 15:
        watch_events.append(
            f"💱 환율 {usd_krw:,.0f}원 ({'+' if usd_krw > _pfx else ''}{usd_krw - _pfx:,.0f}원) — 하루 새 큰 폭 변동")
    if not watch_events:
        watch_events.append("✅ 내일 특별히 챙길 신호는 없습니다")

    # ⑤ 한 줄 요약 — 급락일이 최우선 (레짐 게이트와 동일 모순 방지)
    if kr_avg <= -2.0:
        strategy = "당일 급락, 저가 매수를 자제하고 후속 하락·반대매매 소화를 먼저 확인"
    elif vix >= THR_VIX_HIGH:
        strategy = f"VIX {vix:.0f} 공포 구간, 추격을 멈추고 현금 비중 점검"
    elif market_regime == "risk-off":
        strategy = "위험 회피 국면, 방어적으로 보며 낙폭 과대 업종 관찰"
    elif lag_inds:
        x = lag_inds[0]
        strategy = (f"{x['ind']}{_josa(x['ind'], '은', '는')} 해외가 먼저 올랐습니다"
                    f"(해외 5일 {_fmt(x['us'])}), 국내 수급이 따라붙는지 확인")
    elif len(sync_up) >= 2:
        inds = "·".join(x["ind"] for x in sync_up[:2])
        strategy = f"해외와 한국이 같이 오르는 중입니다({inds})"
    elif lead_overext:
        # 주도 업종이 당일 급등에 집중 → top_ind_str가 이미 근거+되돌림 주의를 말함.
        # 전략은 반복 대신 대응 태도만.
        strategy = "성급한 추격보다 하루 더 확인"
    elif decouple_risk:
        # 디커플(해외만 하락)만으로는 되돌림을 단정하지 않는다 — 사실 전달 + 관찰 권유.
        r = decouple_risk[0]
        strategy = (f"{r['ind']}{_josa(r['ind'], '은', '는')} 해외와 갈라져 국내만 강세"
                    f"(해외 5일 {_fmt(r['us'])}), 지속 여부 관찰")
    elif turn_watch:
        t = turn_watch[0]
        strategy = ((f"{t['ind']}{_josa(t['ind'], '은', '는')} 해외와 5일 방향이 엇갈렸지만 "
                     f"직전 해외 세션이 {_fmt(t['us1'])}, 하루 더 확인")
                    if t["dir"] == "up" else
                    (f"{t['ind']} 해외 반등세가 꺾였습니다({_fmt(t['us1'])}), 국내 추격 근거 약화"))
    else:
        strategy = "해외와 뚜렷한 방향 차이는 없습니다, 업종별 선별 대응"

    # 주도 업종은 '당일', 크로스 신호는 '5일' — 같은 문장에서 기간이 달라
    # 모순처럼 읽히던 문제를 '오늘은'+수치 명시로 해소.
    # 급락일엔 1등도 마이너스라 '주도'가 오독을 부른다 → '버팀'/생략으로 구분.
    top_ind_str = ""
    if kr_sorted:
        _ti, _tc = kr_sorted[0]
        if lead_overext:
            # 주도 업종의 당일 급등이 최근 상승분 대부분(직전 4일 마이너스) → 과열 근거 명시
            top_ind_str = f" · 오늘은 {_ti}({_fmt(_tc)}) 급등(직전 4일 {_fmt(_lead_d5x)}), 되돌림 주의"
        elif _tc > 0 and kr_avg > -2.0:
            top_ind_str = f" · 오늘은 {_ti}({_fmt(_tc)}){_josa(_ti, '이', '가')} 주도"
        elif _tc > 0:
            top_ind_str = f" · {_ti}({_fmt(_tc)})만 버팀"
    # 코스피·코스닥 둘 다 표기 — 코스닥이 더 강한/약한 날(예: 코스피 +4.4% vs 코스닥 +5.2%)
    # 코스피만 보이면 시장을 과소·오표현한다. 강세/약세 라벨은 두 지수 평균(kr_avg) 기준.
    one_line_summary = (f"코스피 {_fmt(kospi_chg)} · 코스닥 {_fmt(kosdaq_chg)} "
                        f"{kr_label}{top_ind_str} — {strategy}")

    return {
        "flow_summary":    flow_summary,
        "key_points":      key_points[:4],
        "risk_factors":    risk_factors[:4],
        "watch_events":    watch_events[:4],
        "strong_industries": strong_inds,
        "weak_industries": weak_inds,
        "top_stocks":      hgpr_names,
        "one_line_summary": one_line_summary,
        "data_basis":      f"{macro.get('base_date','최신')} 시장 데이터 기준",
    }


# ════════════════════════════════════════════════════════════
# 저장
# ════════════════════════════════════════════════════════════

def save_summary(market_date: str, payload: dict) -> bool:
    """market_investment_summary 테이블에 upsert."""
    if not sb:
        log.warning("[save_summary] Supabase 미연결")
        return False
    try:
        record = {
            "market_date":       market_date,
            "market_type":       "KR",
            "one_line_summary":  payload["one_line_summary"],
            "flow_summary":      json.dumps(payload["flow_summary"], ensure_ascii=False),
            "key_points":        json.dumps(payload["key_points"], ensure_ascii=False),
            "risk_factors":      json.dumps(payload["risk_factors"], ensure_ascii=False),
            "watch_events":      json.dumps(payload["watch_events"], ensure_ascii=False),
            "strong_industries": json.dumps(payload["strong_industries"], ensure_ascii=False),
            "weak_industries":   json.dumps(payload["weak_industries"], ensure_ascii=False),
            "top_stocks":        json.dumps(payload["top_stocks"], ensure_ascii=False),
            "data_basis":        payload["data_basis"],
            "generated_at":      datetime.utcnow().isoformat() + "Z",
        }
        sb.table("market_investment_summary").upsert(record, on_conflict="market_date,market_type").execute()
        log.info(f"[save_summary] ✅ {market_date} 저장 완료")
        return True
    except Exception as e:
        log.error(f"[save_summary] 저장 실패: {e}")
        return False


# ════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════

def run(target_date: Optional[str] = None):
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")
    log.info(f"[SummaryGen] 시작 — {target_date}")

    refresh_industry_config()   # us_etf_map 단일 출처 — USKR_MAP·KR_INDUSTRIES 갱신

    macro    = fetch_macro(target_date)
    if not macro:
        log.warning("[SummaryGen] macro_data 없음 — 중단")
        return False

    market_rows = fetch_market_summary(target_date)
    ind_trend   = fetch_industry_trend(target_date)
    us_etf      = fetch_us_etf(target_date, kr_days=ind_trend.get("_days"))
    discs       = fetch_disclosures(target_date)

    log.info(
        f"[SummaryGen] 데이터: 종목 {len(market_rows)}개 / "
        f"US ETF {len(us_etf)}개 / 공시 {len(discs)}건"
    )

    payload = analyze(macro, market_rows, us_etf, ind_trend, discs)
    log.info(f"[SummaryGen] 한줄요약: {payload['one_line_summary']}")

    return save_summary(target_date, payload)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    ok = run(date_arg)
    sys.exit(0 if ok else 1)
