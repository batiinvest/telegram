"""
check_missing_financials.py — 재무데이터 누락 종목 진단
────────────────────────────────────────────────────────
companies 테이블과 financials 테이블을 비교해
재무데이터가 없는 종목을 확인합니다.

사용법:
  python check_missing_financials.py                    # 전체 현황 요약
  python check_missing_financials.py --non-monitored    # 비모니터링 누락만
  python check_missing_financials.py --monitored        # 모니터링 누락만
  python check_missing_financials.py --year 2025 --quarter Q4  # 특정 분기 기준
  python check_missing_financials.py --export missing.txt       # 파일로 저장
"""

import os, sys, argparse, logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')
from logger_config import get_logger
log = get_logger(__name__)

from db_utils import fetch_all_pages
from db_client import get_supabase_client

sb = get_supabase_client()


def get_companies(scope: str) -> list[dict]:
    """scope: 'all' | 'monitored' | 'non_monitored'"""
    q = sb.table('companies').select('code,name,market,is_monitored,corp_code').eq('active', True)
    if scope == 'monitored':
        q = q.eq('is_monitored', True)
    elif scope == 'non_monitored':
        q = q.eq('is_monitored', False)
    return fetch_all_pages(q)


def get_codes_with_financials(year: str, quarter: str) -> set[str]:
    """financials 테이블에서 해당 분기 데이터가 있는 stock_code 집합."""
    rows = fetch_all_pages(
        sb.table('financials')
          .select('stock_code')
          .eq('bsns_year', year)
          .eq('quarter', quarter)
          .eq('fs_div', 'CFS')
    )
    return {r['stock_code'] for r in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--monitored',     action='store_true', help='모니터링 종목만')
    parser.add_argument('--non-monitored', action='store_true', help='비모니터링 종목만')
    parser.add_argument('--year',    type=str, default='2025', help='기준 사업연도 (기본: 2025)')
    parser.add_argument('--quarter', type=str, default='Q4',   help='기준 분기 (기본: Q4)')
    parser.add_argument('--export',  type=str, help='누락 종목 목록을 파일로 저장')
    args = parser.parse_args()

    scope = 'non_monitored' if args.non_monitored else ('monitored' if args.monitored else 'all')
    year, quarter = args.year, args.quarter

    log.info(f'=== 재무데이터 누락 진단: {year} {quarter} / 대상: {scope} ===')

    companies = get_companies(scope)
    log.info(f'대상 종목: {len(companies):,}개')

    codes_with_fin = get_codes_with_financials(year, quarter)
    log.info(f'재무데이터 보유: {len(codes_with_fin):,}개')

    # 코드 정규화 (.KS/.KQ 제거)
    missing = []
    has_data = []
    no_corp_code = []

    for c in companies:
        raw_code = c.get('code', '')
        code = raw_code.replace('.KS', '').replace('.KQ', '')
        if not c.get('corp_code'):
            no_corp_code.append(c)
            continue
        if code in codes_with_fin:
            has_data.append(c)
        else:
            missing.append(c)

    # ── 결과 출력 ──────────────────────────────────────────────────────────────
    print()
    print('=' * 60)
    print(f'  재무데이터 누락 현황 — {year} {quarter} / {scope}')
    print('=' * 60)
    print(f'  전체 대상      : {len(companies):>6,}개')
    print(f'  corp_code 없음  : {len(no_corp_code):>6,}개  (DART 미등록)')
    print(f'  데이터 보유     : {len(has_data):>6,}개')
    print(f'  ★ 데이터 누락  : {len(missing):>6,}개  ← 수집 필요')
    print('=' * 60)

    # 시장별 breakdown
    kospi_missing  = [c for c in missing if c.get('market') == 'KOSPI']
    kosdaq_missing = [c for c in missing if c.get('market') == 'KOSDAQ']
    print(f'  누락 KOSPI     : {len(kospi_missing):>6,}개')
    print(f'  누락 KOSDAQ    : {len(kosdaq_missing):>6,}개')
    print()

    # 모니터링 종목 중 누락이면 경고
    monitored_missing = [c for c in missing if c.get('is_monitored')]
    if monitored_missing:
        print(f'  ⚠️  모니터링 종목 중 누락: {len(monitored_missing)}개')
        for c in monitored_missing[:10]:
            print(f'     - {c["name"]} ({c["code"]})')
        print()

    # 누락 목록 미리보기 (상위 20개)
    if missing:
        print('  누락 종목 미리보기 (상위 20개):')
        for c in missing[:20]:
            mon = '★' if c.get('is_monitored') else ' '
            print(f'   {mon} {c.get("name",""):20s} {c.get("code",""):12s} {c.get("market",""):6s}')
        if len(missing) > 20:
            print(f'   ... 외 {len(missing) - 20}개')
        print()

    # 파일 저장
    if args.export and missing:
        with open(args.export, 'w', encoding='utf-8') as f:
            f.write(f'# 재무데이터 누락 종목 — {year} {quarter} ({scope})\n')
            f.write(f'# 총 {len(missing)}개\n')
            f.write('corp_code\tcode\tname\tmarket\tis_monitored\n')
            for c in missing:
                f.write(f'{c.get("corp_code","")}\t{c.get("code","")}\t'
                        f'{c.get("name","")}\t{c.get("market","")}\t{c.get("is_monitored","")}\n')
        log.info(f'누락 목록 저장: {args.export}')

    # 수집 명령어 안내
    print('  ─── 수집 명령어 ───────────────────────────────────────')
    if scope in ('all', 'non_monitored'):
        print(f'  비모니터링 최근 분기만:')
        print(f'    python collect_financials.py --non-monitored-only')
        print(f'  비모니터링 2023년부터 전체:')
        print(f'    python collect_financials.py --non-monitored-only --from-year 2023')
    if scope in ('all', 'monitored') and monitored_missing:
        print(f'  모니터링 누락 분기:')
        print(f'    python collect_financials.py {year} {quarter} --monitored-only')
    print('=' * 60)


if __name__ == '__main__':
    main()
