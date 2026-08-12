# -*- coding: utf-8 -*-
"""
industry_summary.py — 산업 채팅방 하루 마감 종합 브리핑
────────────────────────────────────────────────────────────────
산업별로 그날 장을 한 화면에 정리해 산업 채팅방에 발송한다.
종목별 요약(daily_summaries)을 산업 단위로 롤업 + 시장지표(sector_daily_summary,
market_data)를 얹는다.

구성(확정): 🧠 한눈에=규칙기반(AI 미사용) · 전 산업 매일 발송 · 수급은 방향만.

실행:
  python3 industry_summary.py                 # 오늘, 산업 채팅방 발송
  python3 industry_summary.py 2026-07-24       # 특정일
  python3 industry_summary.py 2026-07-24 --sample 조선 반도체 바이오   # 개인방 샘플
"""
import sys
import datetime
import logging

import config
from db_client import get_supabase_client
from telegram_utils import get_admin_chat_id
import stock_api

KST = datetime.timezone(datetime.timedelta(hours=9))
WD = ['월', '화', '수', '목', '금', '토', '일']
DART_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

# US ETF 선행 신호 → 한글
SIGNAL_KO = {
    'us_lead_bull': '미국 섹터 선행 강세',
    'us_lead_bear': '미국 섹터 선행 약세',
    'co_bull':      '한·미 동반 강세',
    'co_bear':      '한·미 동반 약세',
}
# 미국 ETF가 매핑된 산업만 US 라인 노출 (없는 산업은 생략)
US_INDUSTRIES = {'반도체', '2차전지', '바이오', '로봇', '신재생', '조선', '뷰티', '테크'}

# 공시 중요도 티어 (낮을수록 우선) — 하루 브리핑 '재료'는 이벤트성이 지분보고보다 중요.
# 대량보유·지분공시(5%↑ 보고, 임원 지분변동)는 routine 노이즈라 최하위로 밀어 필러로만 쓴다.
DISC_TIER = {
    '잠정실적': 0, '공정공시': 0, '주요사항': 0, '주요경영사항': 0,
    '단일판매·공급계약': 0,
    '증자/감자': 1, '합병/분할': 1, '자사주': 1, '사채/전환': 1, '최대주주변동': 1,
    '배당': 2, '기업설명회(IR)': 2,
    '대량보유': 3, '지분공시': 3, '임원/주식': 3, '감사보고서': 3, '기타': 3,
}


def _disc_rank(cat) -> int:
    return DISC_TIER.get(cat, 3)


# ── 포맷 헬퍼 ──────────────────────────────────────────────────

def fmt_pct(v) -> str:
    v = v or 0
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f}%"


def fmt_won(v) -> str:
    v = v or 0
    if v >= 1e12:
        return f"{v / 1e12:.1f}조"
    if v >= 1e8:
        return f"{round(v / 1e8):,}억"
    return f"{round(v / 1e4):,}만"


def flow_phrase(foreign, inst) -> str:
    f, i = foreign or 0, inst or 0
    if f < 0 and i < 0:
        return "외국인·기관 동반 순매도"
    if f > 0 and i > 0:
        return "외국인·기관 동반 순매수"
    if f < 0 and i > 0:
        return "외국인 순매도 · 기관 순매수"
    if f > 0 and i < 0:
        return "외국인 순매수 · 기관 순매도"
    return "수급 중립"


def oneliner(chg, up, dn, foreign, inst, has_material) -> str:
    """🧠 한눈에 — 규칙기반 한 줄. 등락 강도 + breadth + 수급 + 재료."""
    chg = chg or 0
    if chg <= -5:
        base = "업종 전반 급락"
    elif chg <= -2:
        base = "업종 약세"
    elif chg < -0.5:
        base = "소폭 약세"
    elif chg <= 0.5:
        base = "보합권 등락"
    elif chg < 2:
        base = "완만한 강세"
    elif chg < 5:
        base = "업종 강세"
    else:
        base = "업종 전반 급등"

    if dn >= up * 3 and dn >= 5:
        breadth = "대부분 하락"
    elif up >= dn * 3 and up >= 5:
        breadth = "대부분 상승"
    elif dn > up:
        breadth = "하락 우위"
    elif up > dn:
        breadth = "상승 우위"
    else:
        breadth = "혼조"

    flow = flow_phrase(foreign, inst)
    tail = " 속 개별 공시·뉴스 부각." if has_material else "."
    return f"{base}({breadth}), {flow}{tail}"


# ── 산업별 재료 하이라이트 (daily_summaries 롤업) ───────────────

def _best_disc(discs):
    """가장 중요한 공시 1건 (티어 우선)."""
    return min(discs, key=lambda x: _disc_rank(x.get('category'))) if discs else None


def _highlight(row) -> str | None:
    """daily_summaries 한 종목 → 재료 한 줄."""
    corp = row['corp_name']
    items = row.get('items') or {}
    news = items.get('news') or []
    discs = items.get('disclosures') or []

    # 뉴스 우선(가장 많이 보도된 것) — 읽기 좋음
    if news:
        n = news[0]
        more = f" <i>· {n['sources']}개 매체</i>" if n.get('sources', 1) > 1 else ""
        return f"<b>{corp}</b> · <a href='{n['link']}'>{n['title']}</a>{more}"

    # 뉴스 없으면 티어 최상위 공시
    d = _best_disc(discs)
    if d:
        rno = d.get('rcept_no')
        link = f" <a href='{DART_URL.format(rno)}'>DART</a>" if rno else ""
        nm = ' '.join((d.get('report_nm') or '').split())   # 내부 공백 정리
        return f"<b>{corp}</b> · [{d.get('category')}] {nm}{link}"
    return None


def _salience(row) -> tuple:
    """재료 정렬 키(높을수록 우선): 뉴스 有 > 이벤트성 공시 > 지분보고."""
    items = row.get('items') or {}
    news = items.get('news') or []
    discs = items.get('disclosures') or []
    best = _best_disc(discs)
    tier = _disc_rank(best.get('category')) if best else 9
    cnt = (row.get('news_cnt') or 0) + (row.get('disclosure_cnt') or 0)
    return (1 if news else 0, -tier, cnt)


def _has_key_material(mats_rows) -> bool:
    """상위 재료 중 뉴스 또는 이벤트성(티어 0~1) 공시가 하나라도 있는지."""
    for r in mats_rows:
        items = r.get('items') or {}
        if items.get('news'):
            return True
        d = _best_disc(items.get('disclosures') or [])
        if d and _disc_rank(d.get('category')) <= 1:
            return True
    return False


def _materials(summ_rows, limit=4) -> tuple:
    """산업 내 활동 종목을 중요도순 정렬 후 상위 재료. (라인리스트, 핵심재료여부)."""
    rows = sorted(summ_rows, key=_salience, reverse=True)[:limit]
    lines = [h for h in (_highlight(r) for r in rows) if h]
    return lines, _has_key_material(rows)


# ── 메시지 구성 ────────────────────────────────────────────────

def build_message(industry, date_obj, sector, movers, summ_rows) -> str:
    up = sum(1 for m in movers if (m['chg'] or 0) > 0)
    dn = sum(1 for m in movers if (m['chg'] or 0) < 0)
    total = len(movers)

    by_chg = sorted(movers, key=lambda m: m['chg'], reverse=True)
    by_tv = sorted(movers, key=lambda m: m.get('tv') or 0, reverse=True)
    mats, key_material = _materials(summ_rows)

    ds = f"{date_obj.month}/{date_obj.day}({WD[date_obj.weekday()]})"
    L = [f"🌙 <b>[{industry} 마감 브리핑]</b>  {ds}", ""]

    # 업종 등락 + breadth
    chg = sector.get('avg_chg_1d')
    surge = "  ⚠️ 급락" if (chg or 0) <= -5 else ("  🔥 급등" if (chg or 0) >= 5 else "")
    L.append(f"📊 업종 <b>{fmt_pct(chg)}</b>{surge}   상승 {up} · 하락 {dn} ({total}종목)")

    # 미국 참고 (매핑된 산업만)
    if industry in US_INDUSTRIES and sector.get('us_chg_1d') is not None:
        sig = sector.get('signal_20d') or sector.get('signal_5d')
        sig_ko = f"  · {SIGNAL_KO[sig]}" if sig in SIGNAL_KO else ""
        L.append(f"🇺🇸 미국 {industry} ETF {fmt_pct(sector.get('us_chg_1d'))}{sig_ko}")

    # 수급 (방향만)
    L.append(f"💰 수급  {flow_phrase(sector.get('foreign_net_1d'), sector.get('inst_net_1d'))}")

    # 거래대금 상위
    if by_tv and (by_tv[0].get('tv') or 0) > 0:
        tv3 = " · ".join(f"{m['name']} {fmt_won(m['tv'])}" for m in by_tv[:3] if (m.get('tv') or 0) > 0)
        L.append(f"💵 거래대금  {tv3}")

    # 상승/하락
    L.append("")
    if up:
        L.append("▲ " + "  ".join(f"{m['name']} {fmt_pct(m['chg'])}" for m in by_chg[:3] if (m['chg'] or 0) > 0))
    if dn:
        L.append("▼ " + "  ".join(f"{m['name']} {fmt_pct(m['chg'])}" for m in by_chg[::-1][:3] if (m['chg'] or 0) < 0))

    # 오늘의 공시·뉴스 (종목 리포트 카드와 용어 통일)
    L.append("")
    if mats:
        L.append("📌 <b>오늘의 공시·뉴스</b>")
        L += [f" • {m}" for m in mats]
    else:
        L.append("📌 <b>오늘의 공시·뉴스</b>  특이사항 없음")

    # 한눈에 (규칙기반)
    L.append("")
    L.append(f"🧠 {oneliner(chg, up, dn, sector.get('foreign_net_1d'), sector.get('inst_net_1d'), key_material)}")
    return "\n".join(L)


# ── 데이터 로드 ────────────────────────────────────────────────

def _load(sb, base_date):
    sectors = {r['industry']: r for r in
               (sb.table('sector_daily_summary').select('*').eq('base_date', base_date).execute().data or [])}

    # 모니터링 종목 code→(name,industry)
    code_meta = {config.COMPANY_CODES[n]: (n, config.COMPANY_TO_INDUSTRY.get(n))
                 for n in config.COMPANY_CODES}
    md = sb.table('market_data') \
           .select('stock_code,corp_name,price_change_rate,trading_value') \
           .eq('base_date', base_date).in_('stock_code', list(code_meta.keys())).execute().data or []
    movers_by_ind = {}
    for m in md:
        if m.get('price_change_rate') is None:
            continue
        name, ind = code_meta.get(m['stock_code'], (m.get('corp_name'), None))
        if not ind:
            continue
        movers_by_ind.setdefault(ind, []).append(
            {'name': m.get('corp_name') or name, 'chg': m['price_change_rate'], 'tv': m.get('trading_value')})

    summ_by_ind = {}
    for r in (sb.table('daily_summaries')
              .select('corp_name,industry,disclosure_cnt,news_cnt,is_major,items')
              .eq('base_date', base_date).execute().data or []):
        summ_by_ind.setdefault(r.get('industry'), []).append(r)

    return sectors, movers_by_ind, summ_by_ind


def generate(base_date=None, to=None, only=None, sample=False):
    base_date = base_date or datetime.datetime.now(KST).date().isoformat()
    date_obj = datetime.date.fromisoformat(base_date)
    sb = get_supabase_client()
    sectors, movers_by_ind, summ_by_ind = _load(sb, base_date)

    industries = only or sorted(sectors.keys())
    if not industries:
        logging.warning("⚠️ [산업브리핑] sector_daily_summary 없음 — 중단")
        return 0

    if sample and to:
        stock_api.send_telegram(to,
            f"🌆 <b>[샘플] 산업 마감 브리핑</b>\n{base_date} 기준 · {len(industries)}개 산업\n<i>※ 포맷 검토용</i>",
            preview=False)

    sent = 0
    for ind in industries:
        sector = sectors.get(ind)
        if not sector:
            continue
        movers = movers_by_ind.get(ind, [])
        summ = summ_by_ind.get(ind, [])
        msg = build_message(ind, date_obj, sector, movers, summ)

        target = to if to else config.INDUSTRY_CHAT_IDS.get(ind)
        if not target:
            logging.info(f"ℹ️ [산업브리핑] {ind} 채팅방 없음 — 스킵")
            continue
        ok = stock_api.send_telegram(target, msg, preview=False)
        sent += 1 if ok else 0
        print(f"  • {ind}: 종목 {len(movers)} / 재료 {len(summ)} → {'발송' if ok else '실패'}")
    logging.info(f"🌆 [산업브리핑] {base_date} — {sent}개 발송")
    return sent


def run():
    """스케줄러 진입점 — 산업 채팅방 발송."""
    return generate()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    args = sys.argv[1:]
    base_date = next((a for a in args if a[:2] == '20' and '-' in a), None)
    if '--sample' in args:
        i = args.index('--sample')
        only = [a for a in args[i + 1:] if not a.startswith('-')] or None
        admin = get_admin_chat_id()
        print(f"샘플 발송 → {admin}")
        generate(base_date, to=admin, only=only, sample=True)
    else:
        print(f"발송 완료: {generate(base_date)}개 산업")
