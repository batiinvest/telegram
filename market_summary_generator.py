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

import os
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
    """macro_data 테이블에서 최신 1건 조회."""
    if not sb:
        return {}
    try:
        res = sb.table("macro_data").select("*") \
            .lte("base_date", target_date) \
            .order("base_date", desc=True).limit(1).execute()
        return (res.data or [{}])[0]
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


def fetch_us_etf(target_date: str) -> dict:
    """
    us_market 테이블에서 최근 5거래일 US ETF 산업 평균 등락률 조회.
    반환: { industry: { d1: 당일 평균, d5: 5일 복리 누적 } }
    d5 = 일별 등가중 평균의 복리 누적 — 프론트 buildInsightData(market-insight.js)와 동일 기준.
    """
    if not sb:
        return {}
    try:
        res = sb.table("us_market")             .select("base_date,industry,ticker,chg_pct")             .lte("base_date", target_date)             .order("base_date", desc=True).limit(600).execute()
        rows = res.data or []
        if not rows:
            return {}
        last5 = sorted({r["base_date"] for r in rows})[-5:]
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
        result = {}
        for ind in KR_INDUSTRIES:
            d1 = None
            cum = 100.0
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
            d5 = round(cum - 100, 2) if has_any else None
            result[ind] = {"d1": d1, "d5": d5}
        return result
    except Exception as e:
        log.warning(f"[fetch_industry_trend] {e}")
        return {}


# ════════════════════════════════════════════════════════════
# 분석 로직
# ════════════════════════════════════════════════════════════

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

    # ── US 크로스 신호 — 5일 누적 기준(단일일 노이즈 제거, 프론트 S5와 동일) ──
    sync_up, lag_inds, decouple_risk, sync_down = [], [], [], []
    for ind in KR_INDUSTRIES:
        us5 = (us_etf.get(ind) or {}).get("d5")
        kr5 = ind_trend.get(ind, {}).get("d5")
        if us5 is None or kr5 is None:
            continue
        if us5 >= THR_S5 and kr5 <= -THR_S5:
            lag_inds.append({"ind": ind, "us": us5, "kr": kr5})
        elif us5 <= -THR_S5 and kr5 >= THR_S5:
            decouple_risk.append({"ind": ind, "us": us5, "kr": kr5})
        elif us5 >= THR_S5 and kr5 >= THR_S5:
            sync_up.append({"ind": ind, "us": us5, "kr": kr5})
        elif us5 <= -THR_S5 and kr5 <= -THR_S5:
            sync_down.append({"ind": ind, "us": us5, "kr": kr5})

    # ── 공시 분석 ──
    from collections import Counter
    disc_count = len(discs)
    cat_counter = Counter(d.get("category","기타") for d in discs)
    top_cats = [f"{k}({v})" for k, v in cat_counter.most_common(3)]

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
        inds = " / ".join(f"{x['ind']}(미국 5일 {_fmt(x['us'])}→한국 5일 {_fmt(x['kr'])})" for x in lag_inds[:2])
        if defensive:
            key_points.append(f"후행 관찰 후보: {inds} — 급락 진정·수급 유입 확인 후 진입 검토")
        else:
            key_points.append(f"후행 선점 후보: {inds} — 수급 유입 시 진입 검토")
    if len(sync_up) >= 2:
        inds = " · ".join(x["ind"] for x in sync_up[:3])
        key_points.append(f"미·한 5일 동반 강세: {inds} — " + ("반등 확인 후 재평가" if defensive else "추세 추종 검토"))
    if hgpr_count >= 3:
        nm = " · ".join(hgpr_names[:2])
        key_points.append(f"52주 신고가 {hgpr_count}개 — {nm}{'등' if hgpr_count > 2 else ''}")
    if top_frgn and kospi_chg > 0:
        key_points.append(f"외국인 순매수 집중: {' · '.join(top_frgn[:2])}")
    if top_tv:
        key_points.append(f"거래대금 상위: {' · '.join(top_tv[:3])}")
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
        risk_factors.append(f"⚡ 디커플링: {r['ind']}(한국 5일 {_fmt(r['kr'])} / 미국 5일 {_fmt(r['us'])})")
    if sync_down:
        inds = " · ".join(x["ind"] for x in sync_down[:3])
        risk_factors.append(f"🔻 미·한 5일 동반 약세: {inds}")
    if us10y >= THR_US10Y:
        risk_factors.append(f"📊 미 10년 금리 {us10y:.3f}% — 고금리 부담")
    if usd_krw and usd_krw >= 1450:
        risk_factors.append(f"💱 환율 {usd_krw:,.0f}원 — 고환율 부담")
    if not risk_factors:
        risk_factors.append("✅ 현재 주요 리스크 신호 없음")

    # ④ 확인할 이벤트
    watch_events = []
    if disc_count > 0:
        cats = " · ".join(top_cats)
        watch_events.append(f"📋 오늘 공시 {disc_count}건 ({cats})")
    if sp500_fut_chg is not None:
        dir_str = "상승" if sp500_fut_chg >= 0.3 else ("하락" if sp500_fut_chg <= -0.3 else "보합")
        watch_events.append(f"🇺🇸 S&P500 선물 {_fmt(sp500_fut_chg)} {dir_str}")
    if vix:
        watch_events.append(f"📊 VIX {vix:.1f}, 미 10년 금리 {us10y:.3f}%")
    if not watch_events:
        watch_events.append("확인 이벤트 없음")

    # ⑤ 한 줄 요약 — 급락일이 최우선 (레짐 게이트와 동일 모순 방지)
    if kr_avg <= -2.0:
        strategy = "당일 급락 — 저가 매수 자제, 후속 하락·반대매매 소화 확인 우선"
    elif vix >= THR_VIX_HIGH:
        strategy = f"VIX {vix:.0f} 공포 구간 — 추격 금지, 현금 비중 점검"
    elif market_regime == "risk-off":
        strategy = "리스크오프 — 방어 포지션 유지, 낙폭 과대 관찰"
    elif lag_inds:
        inds = "·".join(x["ind"] for x in lag_inds[:2])
        strategy = f"후행 업종({inds}) 선점 기회 점검"
    elif len(sync_up) >= 2:
        inds = "·".join(x["ind"] for x in sync_up[:2])
        strategy = f"미·한 동반 강세 — 강세 업종({inds}) 집중 대응"
    elif decouple_risk:
        inds = "·".join(x["ind"] for x in decouple_risk[:2])
        strategy = f"디커플링({inds}) 차익 실현 검토"
    else:
        strategy = "혼조 국면 — 추격보다 관찰, 후행 업종 선별 대응"

    top_ind_str = f", {kr_sorted[0][0]} 주도" if kr_sorted else ""
    one_line_summary = f"코스피 {_fmt(kospi_chg)} {kr_label}{top_ind_str} — {strategy}"

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
        log.warning(f"[SummaryGen] macro_data 없음 — 중단")
        return False

    market_rows = fetch_market_summary(target_date)
    us_etf      = fetch_us_etf(target_date)
    ind_trend   = fetch_industry_trend(target_date)
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
