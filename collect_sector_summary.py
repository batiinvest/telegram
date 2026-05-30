"""
collect_sector_summary.py — 산업별 일별 요약 계산 및 저장
─────────────────────────────────────────────────────────
모니터링 종목 기준으로 산업별 기간별 집계값을 계산해
sector_daily_summary 테이블에 upsert합니다.

저장 컬럼:
  avg_chg_1d/5d/20d   : 산업 평균 등락률 (일별 평균의 누적합)
  foreign_net_1d/5d/20d : 외국인 순매수 합산 (백만원)
  inst_net_1d/5d/20d    : 기관 순매수 합산 (백만원)
  stock_count           : 집계 종목 수

Supabase SQL (최초 1회 실행):
──────────────────────────────
CREATE TABLE IF NOT EXISTS sector_daily_summary (
  base_date       DATE    NOT NULL,
  industry        TEXT    NOT NULL,
  avg_chg_1d      NUMERIC,
  avg_chg_5d      NUMERIC,
  avg_chg_20d     NUMERIC,
  foreign_net_1d  BIGINT,
  foreign_net_5d  BIGINT,
  foreign_net_20d BIGINT,
  inst_net_1d     BIGINT,
  inst_net_5d     BIGINT,
  inst_net_20d    BIGINT,
  stock_count     INT,
  PRIMARY KEY (base_date, industry)
);

사용법:
  python collect_sector_summary.py          # 최신 날짜 실행
  python collect_sector_summary.py 2026-05-28  # 특정 날짜
"""

import os, sys, logging
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

from supabase import create_client
from db_utils import fetch_all_pages

_URL = os.environ['SUPABASE_URL']
_KEY = os.environ.get('SUPABASE_KEY', os.environ.get('SUPABASE_SERVICE_ROLE_KEY', ''))
sb   = create_client(_URL, _KEY)

PERIODS = [1, 5, 20]   # 집계 기간 (거래일)
LOOKBACK = 25          # 최근 N 거래일 조회


# ── 거래일 목록 조회 ──────────────────────────────────────────────────────────
def get_trading_days(base_date: str) -> list[str]:
    """base_date 기준 최근 LOOKBACK 거래일 목록 (최신→구) 반환."""
    rows = sb.from_('macro_data') \
        .select('base_date') \
        .lte('base_date', base_date) \
        .order('base_date', desc=True) \
        .limit(LOOKBACK) \
        .execute().data or []
    return [r['base_date'] for r in rows]  # [최신, ..., 구]


# ── 산업 매핑 조회 ────────────────────────────────────────────────────────────
def get_industry_map() -> dict[str, str]:
    """모니터링 종목 code → industry 매핑."""
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


# ── 시장 데이터 조회 ──────────────────────────────────────────────────────────
def get_market_data(codes: list[str], cutoff: str, base_date: str) -> list[dict]:
    """모니터링 종목의 기간 내 등락률·수급 데이터 조회."""
    return fetch_all_pages(
        sb.from_('market_data')
          .select('base_date,stock_code,price_change_rate,foreign_net_buy,institution_net_buy')
          .in_('stock_code', codes)
          .gte('base_date', cutoff)
          .lte('base_date', base_date)
    )


# ── 집계 계산 ─────────────────────────────────────────────────────────────────
def calc_summary(trading_days: list[str], market_rows: list[dict],
                 ind_map: dict[str, str]) -> list[dict]:
    """
    trading_days[0] = 최신 날짜
    반환: sector_daily_summary upsert용 레코드 리스트
    """
    base_date = trading_days[0]
    # 날짜 → 인덱스 (0 = 최신)
    day_idx = {d: i for i, d in enumerate(trading_days)}

    # industry → period → 합산 버킷
    # buckets[ind][period] = {'chg': [], 'frgn': 0, 'inst': 0}
    buckets: dict[str, dict[int, dict]] = {}

    for row in market_rows:
        ind = ind_map.get(row['stock_code'])
        if not ind:
            continue
        idx = day_idx.get(row['base_date'])
        if idx is None:
            continue

        if ind not in buckets:
            buckets[ind] = {p: {'chg': [], 'frgn': 0, 'inst': 0, 'cnt': 0}
                            for p in PERIODS}

        chg  = row.get('price_change_rate') or 0
        frgn = row.get('foreign_net_buy') or 0
        inst = row.get('institution_net_buy') or 0

        for p in PERIODS:
            if idx < p:   # 0~(p-1) 인덱스 = 최근 p 거래일
                b = buckets[ind][p]
                if row.get('price_change_rate') is not None:
                    b['chg'].append(chg)
                b['frgn'] += frgn
                b['inst'] += inst
                b['cnt']  += 1

    records = []
    all_industries = set(ind_map.values())
    for ind in all_industries:
        b = buckets.get(ind, {})
        rec = {'base_date': base_date, 'industry': ind}
        for p in PERIODS:
            pb = b.get(p, {})
            chg_list = pb.get('chg', [])
            rec[f'avg_chg_{p}d']      = round(sum(chg_list) / len(chg_list), 3) if chg_list else None
            rec[f'foreign_net_{p}d']  = pb.get('frgn') or None
            rec[f'inst_net_{p}d']     = pb.get('inst') or None
        rec['stock_count'] = b.get(1, {}).get('cnt', 0)
        records.append(rec)

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

    codes   = list(ind_map.keys())
    cutoff  = trading_days[-1]
    mkt     = get_market_data(codes, cutoff, target_date)
    log.info(f'market_data {len(mkt):,}행 조회')

    records = calc_summary(trading_days, mkt, ind_map)
    save(records)
    log.info(f'=== 완료: {len(records)}개 산업 저장 ===')


if __name__ == '__main__':
    target = next((a for a in sys.argv[1:] if not a.startswith('--')), None)
    run(target)
