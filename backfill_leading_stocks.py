#!/usr/bin/env python3
"""
backfill_leading_stocks.py
────────────────────────────
leading_stocks 테이블 결손일 백필.

배경: leading_stocks_generator.py가 2026-06-05에 entry_price·price_chg_60d·
sector_strength 컬럼 저장을 추가했으나, DB 테이블에 컬럼이 없어(이후 PostgREST
스키마 캐시 미반영까지 겹쳐) 2026-06-06 ~ 2026-06-15 사이 일배치가 전부 실패함.
컬럼 추가 + 캐시 리로드 완료 후, market_data에는 있지만 leading_stocks에는
없는 거래일만 찾아 leading_stocks_generator.run(date)을 순차 재실행한다.

사용법:
    python backfill_leading_stocks.py                       # 결손일 자동 탐지 후 전체 백필
    python backfill_leading_stocks.py --from 2026-06-06 --to 2026-06-15   # 범위 지정
    python backfill_leading_stocks.py --dry-run              # 대상 날짜만 출력, 실행 안 함
"""

import argparse
import time
from db_client import get_supabase_client
from db_utils import fetch_all_pages
from logger_config import get_logger
from leading_stocks_generator import run as run_leading

log = get_logger(__name__)


def get_existing_dates(sb) -> set[str]:
    rows = fetch_all_pages(sb.table('leading_stocks').select('base_date'))
    return {r['base_date'] for r in rows}


def get_market_dates(sb, from_date: str | None, to_date: str | None) -> list[str]:
    q = sb.table('market_data').select('base_date')
    if from_date:
        q = q.gte('base_date', from_date)
    if to_date:
        q = q.lte('base_date', to_date)
    rows = fetch_all_pages(q)  # PostgREST 1000행 한도 -> 날짜 누락 방지
    return sorted({r['base_date'] for r in rows})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from', dest='from_date', default=None, help='시작일 YYYY-MM-DD (생략 시 자동 탐지)')
    parser.add_argument('--to',   dest='to_date',   default=None, help='종료일 YYYY-MM-DD (생략 시 자동 탐지)')
    parser.add_argument('--dry-run', action='store_true', help='대상 날짜만 출력하고 실행하지 않음')
    args = parser.parse_args()

    sb = get_supabase_client()

    existing = get_existing_dates(sb)
    log.info(f'leading_stocks 기존 저장일: {len(existing)}일')

    market_dates = get_market_dates(sb, args.from_date, args.to_date)
    missing = [d for d in market_dates if d not in existing]

    if not missing:
        log.info('결손일 없음 — 백필 불필요')
        return

    log.info(f'백필 대상: {len(missing)}일 — {missing[0]} ~ {missing[-1]}')
    for d in missing:
        log.info(f'  - {d}')

    if args.dry_run:
        log.info('(dry-run) 실제 실행하지 않음')
        return

    ok, fail = 0, 0
    for d in missing:
        log.info(f'=== 백필 실행: {d} ===')
        try:
            run_leading(d)
            ok += 1
        except Exception as e:
            log.error(f'❌ {d} 백필 실패: {e}')
            fail += 1
        time.sleep(2)  # API 부담 분산

    log.info(f'=== 백필 완료: 성공 {ok} / 실패 {fail} / 총 {len(missing)}일 ===')


if __name__ == '__main__':
    main()
