"""
collect_sector_summary.py — 산업별 일별 요약 + 신호탐지 계산 및 저장
──────────────────────────────────────────────────────────────────────
모니터링 종목(KR) + US ETF 기준으로 산업별 기간별 집계값 및 신호를 계산해
sector_daily_summary 테이블에 upsert합니다.

저장 컬럼:
  avg_chg_1d/5d/20d    : KR 산업 평균 등락률 (%)
  us_chg_1d/5d/20d     : US ETF 산업별 누적 등락률 (%)
  foreign_net_Nd        : 외국인 순매수 합산 (백만원)
  inst_net_Nd           : 기관 순매수 합산 (백만원)
  signal_1d/5d/20d     : 신호 키 문자열 (us_lead_bull|us_lead_bear|kr_outrun|co_bull|co_bear|None)
  stock_count           : 집계 종목 수

신호 판단 로직 (industry-matrix.js _imDetect() 동일):
  us_lead_bull : US > base  AND  (US-KR) > lead   → KR 추격 예상
  us_lead_bear : US < -base AND  (KR-US) > lead   → KR 하락 경고
  kr_outrun    : (KR-US) > lead AND KR > base*0.6 → KR 독주
  co_bull      : US > base*0.7 AND KR > base*0.7  → 동조 강세
  co_bear      : US < -base*0.7 AND KR < -base*0.7 → 동조 약세

Supabase SQL (최초 1회 실행):
──────────────────────────────
CREATE TABLE IF NOT EXISTS sector_daily_summary (
  base_date DATE, industry TEXT,
  avg_chg_1d NUMERIC, avg_chg_5d NUMERIC, avg_chg_20d NUMERIC,
  us_chg_1d  NUMERIC, us_chg_5d  NUMERIC, us_chg_20d  NUMERIC,
  foreign_net_1d BIGINT, foreign_net_5d BIGINT, foreign_net_20d BIGINT,
  inst_net_1d    BIGINT, inst_net_5d    BIGINT, inst_net_20d    BIGINT,
  signal_1d TEXT, signal_5d TEXT, signal_20d TEXT,
  stock_count INT,
  PRIMARY KEY (base_date, industry)
);

사용법:
  python collect_sector_summary.py            # 최신 날짜
  python collect_sector_summary.py 2026-05-28  # 특정 날짜
"""

import sys
from dotenv import load_dotenv

from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')
from logger_config import get_logger
log = get_logger(__name__)

from db_utils import fetch_all_pages
from db_client import get_supabase_client

sb = get_supabase_client()

# ── 기간 / 신호 상수 ──────────────────────────────────────────────────────────
PERIODS  = [1, 5, 20]
LOOKBACK = 25

_THRESH = {
    1:  {'base': 1.0, 'lead': 0.8},
    5:  {'base': 2.5, 'lead': 2.0},
    20: {'base': 5.0, 'lead': 4.0},
}


# ── 신호 탐지 (JS _imDetect() 동일 로직) ────────────────────────────────────
def detect_signal(us_v: float | None, kr_v: float | None, period: int) -> str | None:
    if us_v is None or kr_v is None:
        return None
    thr  = _THRESH.get(period, _THRESH[5])
    base = thr['base']
    lead = thr['lead']

    if us_v > base and (us_v - kr_v) > lead:
        return 'us_lead_bull'
    if us_v < -base and (kr_v - us_v) > lead:
        return 'us_lead_bear'
    if (kr_v - us_v) > lead and kr_v > base * 0.6:
        return 'kr_outrun'
    if us_v > base * 0.7 and kr_v > base * 0.7:
        return 'co_bull'
    if us_v < -base * 0.7 and kr_v < -base * 0.7:
        return 'co_bear'
    return None


# ── 거래일 목록 ───────────────────────────────────────────────────────────────
def get_trading_days(base_date: str) -> list[str]:
    """base_date 기준 최근 LOOKBACK 거래일 (최신→구).

    소스 macro_data → market_data 교체(2026-07-11): macro_data는 주말·휴장일에도
    base_date=당일로 저장돼 거래일 목록이 오염됐고, 5d/20d 윈도우에 비거래일이 끼어
    실제로는 3/13거래일만 집계되는 왜곡이 있었다. market_data에는 KR 거래일만 존재.
    PostgREST는 distinct 미지원 → 최신 날짜부터 1건씩 내려가며 수집 (LOOKBACK회 경량 쿼리).
    """
    days: list[str] = []
    cur = base_date
    first = True
    for _ in range(LOOKBACK):
        q = sb.from_('market_data').select('base_date') \
              .order('base_date', desc=True).limit(1)
        q = q.lte('base_date', cur) if first else q.lt('base_date', cur)
        rows = q.execute().data or []
        if not rows:
            break
        cur = rows[0]['base_date']
        days.append(cur)
        first = False
    return days


# ── 산업 매핑 ─────────────────────────────────────────────────────────────────
def get_industry_map() -> dict[str, str]:
    rows = fetch_all_pages(
        sb.from_('companies')
          .select('code,industry')
          .eq('is_monitored', True)
          .not_.is_('industry', 'null')
    )
    return {
        r['code'].replace('.KS', '').replace('.KQ', ''): r['industry']
        for r in rows if r.get('code') and r.get('industry')
    }


# ── KR 시장 데이터 ────────────────────────────────────────────────────────────
def get_kr_data(codes: list[str], cutoff: str, base_date: str) -> list[dict]:
    return fetch_all_pages(
        sb.from_('market_data')
          .select('base_date,stock_code,price,price_change_rate,foreign_net_buy,institution_net_buy')
          .in_('stock_code', codes)
          .gte('base_date', cutoff)
          .lte('base_date', base_date)
    )


# ── US ETF 데이터 ─────────────────────────────────────────────────────────────
def get_us_data(cutoff: str, base_date: str) -> list[dict]:
    return fetch_all_pages(
        sb.from_('us_market')
          .select('base_date,industry,chg_pct')
          .gte('base_date', cutoff)
          .lte('base_date', base_date)
          .not_.is_('chg_pct', 'null')
    )


# ── US ETF 누적 등락률 계산 ───────────────────────────────────────────────────
def calc_us_chg(trading_days: list[str], us_rows: list[dict]) -> dict[str, dict[int, float | None]]:
    """
    US ETF 산업별 기간별 누적 등락률 — US 자체 세션 기준.
    미국장은 KST 새벽에 마감되므로 us_market 날짜(US 세션일)는 KR 거래일보다 ~1일 빠르다.
    KR 거래일(day_idx)에 맞추면 최신 US 세션이 period=1에서 누락되어 us_chg_1d가 항상 비므로,
    US 자체 날짜를 최신순으로 재랭킹해 'US 최근 period 세션' 기준으로 집계한다.
    반환: {industry: {1: val, 5: val, 20: val}}
    """
    ind_date: dict[str, dict[str, list]] = {}
    for r in us_rows:
        ind = r.get('industry')
        d   = r.get('base_date')
        chg = r.get('chg_pct')
        if not ind or not d or chg is None:
            continue
        ind_date.setdefault(ind, {}).setdefault(d, []).append(chg)

    result: dict[str, dict[int, float | None]] = {}
    for ind, date_vals in ind_date.items():
        us_days = sorted(date_vals.keys(), reverse=True)
        us_idx  = {d: i for i, d in enumerate(us_days)}
        result[ind] = {}
        for period in PERIODS:
            total, count = 0.0, 0
            for d, vals in date_vals.items():
                if us_idx[d] < period:
                    total += sum(vals) / len(vals)
                    count += 1
            result[ind][period] = round(total, 2) if count > 0 else None
    return result


# ── KR 집계 + 신호 계산 ───────────────────────────────────────────────────────
def calc_summary(trading_days: list[str],
                 kr_rows: list[dict],
                 ind_map: dict[str, str],
                 us_chg: dict[str, dict[int, float | None]]) -> list[dict]:
    base_date = trading_days[0]
    day_idx   = {d: i for i, d in enumerate(trading_days)}

    # buckets[ind][period] = {'chg': [], 'frgn': 0, 'inst': 0, 'cnt': 0}
    buckets: dict[str, dict[int, dict]] = {}

    for row in kr_rows:
        ind = ind_map.get(row['stock_code'])
        if not ind:
            continue
        idx = day_idx.get(row['base_date'])
        if idx is None:
            continue

        if ind not in buckets:
            buckets[ind] = {p: {'chg': [], 'frgn': 0, 'inst': 0, 'cnt': 0}
                            for p in PERIODS}

        chg   = row.get('price_change_rate') or 0
        price = row.get('price') or 0
        # 주수 × 가격 / 1e6 = 백만원 단위 (fmtNet 기준)
        frgn  = (row.get('foreign_net_buy')    or 0) * price / 1_000_000
        inst  = (row.get('institution_net_buy') or 0) * price / 1_000_000

        for p in PERIODS:
            if idx < p:
                b = buckets[ind][p]
                if row.get('price_change_rate') is not None:
                    b['chg'].append(chg)
                b['frgn'] += frgn
                b['inst'] += inst
                b['cnt']  += 1

    records = []
    all_industries = set(ind_map.values())

    for ind in all_industries:
        b    = buckets.get(ind, {})
        us_d = us_chg.get(ind, {})
        rec  = {'base_date': base_date, 'industry': ind}

        for p in PERIODS:
            pb       = b.get(p, {})
            chg_list = pb.get('chg', [])
            kr_avg   = round(sum(chg_list) / len(chg_list), 3) if chg_list else None
            us_avg   = us_d.get(p)

            # foreign_net_Nd / inst_net_Nd 컬럼은 BIGINT — '× price / 1e6'(백만원 환산)이
            # 소수 float를 내므로 반드시 int로 반올림해야 한다. (소수 float 그대로 upsert하면
            # PostgreSQL 22P02 'invalid input syntax for type bigint'로 save() 전체가 실패 →
            # sector_daily_summary 갱신이 멈춘 근본 원인. 06-09 단위수정 이후 발생.)
            _frgn = pb.get('frgn')
            _inst = pb.get('inst')
            rec[f'avg_chg_{p}d']     = kr_avg
            rec[f'us_chg_{p}d']      = us_avg
            rec[f'foreign_net_{p}d'] = int(round(_frgn)) if _frgn else None
            rec[f'inst_net_{p}d']    = int(round(_inst)) if _inst else None
            rec[f'signal_{p}d']      = detect_signal(us_avg, kr_avg, p)

        rec['stock_count'] = b.get(1, {}).get('cnt', 0)
        records.append(rec)

    # 요약 로그
    signals = [(r['industry'], p, r[f'signal_{p}d'])
               for r in records for p in PERIODS if r.get(f'signal_{p}d')]
    if signals:
        log.info(f'신호 탐지: {len(signals)}건')
        for ind, p, sig in signals:
            log.info(f'  [{p}d] {ind}: {sig}')
    else:
        log.info('신호 탐지: 없음 (모두 중립)')

    return records


# ── 저장 ─────────────────────────────────────────────────────────────────────
def save(records: list[dict]):
    if not records:
        log.info('저장할 데이터 없음')
        return
    for i in range(0, len(records), 50):
        sb.from_('sector_daily_summary') \
          .upsert(records[i:i+50], on_conflict='base_date,industry') \
          .execute()
    log.info(f'sector_daily_summary upsert: {len(records)}개')


# ── 메인 ─────────────────────────────────────────────────────────────────────
def run(target_date: str | None = None):
    if not target_date:
        res = sb.from_('market_data').select('base_date') \
            .order('base_date', desc=True).limit(1).execute()
        target_date = (res.data or [{}])[0].get('base_date')
    if not target_date:
        log.error('market_data 최신 날짜 없음'); return

    log.info(f'=== sector_daily_summary 계산: {target_date} ===')

    trading_days = get_trading_days(target_date)
    if not trading_days:
        log.error('거래일 없음'); return

    ind_map = get_industry_map()
    if not ind_map:
        log.error('산업 매핑 없음'); return

    codes  = list(ind_map.keys())
    cutoff = trading_days[-1]

    kr_rows = get_kr_data(codes, cutoff, target_date)
    log.info(f'KR market_data {len(kr_rows):,}행')

    us_rows = get_us_data(cutoff, target_date)
    log.info(f'US ETF us_market {len(us_rows):,}행')

    us_chg  = calc_us_chg(trading_days, us_rows)
    records = calc_summary(trading_days, kr_rows, ind_map, us_chg)
    save(records)
    log.info(f'=== 완료: {len(records)}개 산업 저장 ===')


if __name__ == '__main__':
    target = next((a for a in sys.argv[1:] if not a.startswith('--')), None)
    run(target)
