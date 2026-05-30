#!/usr/bin/env python3
"""
특정 날짜 공시 기반 재무수집 테스트
사용법:
  python3 test_disclosure.py --date 20260428   # 특정 날짜
  python3 test_disclosure.py --date 20260425   # 지난주 금요일
  python3 test_disclosure.py --dry-run         # 실제 수집 없이 공시 목록만 확인
"""
import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os, sys, json, argparse, logging
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

try:
    import OpenDartReader
    dart = OpenDartReader(os.getenv('DART_API_KEY',''))
except ImportError:
    print("pip install OpenDartReader"); sys.exit(1)

try:
    from supabase import create_client
    sb = create_client(os.getenv('SB_URL',''), os.getenv('SB_SERVICE_KEY',''))
except Exception as e:
    print(f"Supabase 연결 실패: {e}"); sys.exit(1)

sys.path.insert(0, '/home/kjhofone')
from collect_financials import run_by_corp_codes, auto_detect_quarter


def get_disclosures(target_date: str):
    """특정 날짜 분기/반기/사업보고서 공시 목록 조회"""
    log.info(f"📋 {target_date} 공시 목록 조회 중...")
    try:
        disc = dart.list(start=target_date, end=target_date, final=True)
    except Exception as e:
        log.error(f"공시 조회 실패: {e}")
        return None

    if disc is None or disc.empty:
        log.info("공시 없음")
        return None

    target_types = ['분기보고서', '반기보고서', '사업보고서']
    filtered = disc[disc['report_nm'].str.contains('|'.join(target_types), na=False)]

    log.info(f"전체 공시: {len(disc)}건 → 재무 관련: {len(filtered)}건")
    return filtered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', type=str, help='조회 날짜 (YYYYMMDD), 기본: 지난주 금요일')
    parser.add_argument('--dry-run', action='store_true', help='공시 목록만 확인 (수집 안 함)')
    args = parser.parse_args()

    # 날짜 결정
    if args.date:
        target_date = args.date
    else:
        # 지난주 금요일
        today = date.today()
        days_since_friday = (today.weekday() - 4) % 7
        last_friday = today - timedelta(days=days_since_friday + 7)
        target_date = last_friday.strftime('%Y%m%d')

    log.info(f"테스트 날짜: {target_date}")

    # 공시 목록 조회
    filtered = get_disclosures(target_date)
    if filtered is None or filtered.empty:
        log.info("재무 관련 공시 없음 — 종료")
        return

    # 공시 목록 출력
    print(f"\n{'='*60}")
    print(f"  {target_date} 실적 공시 종목 ({len(filtered)}개)")
    print(f"{'='*60}")
    for _, row in filtered.iterrows():
        print(f"  [{row['report_nm'][:6]}] {row['corp_name']} ({row['corp_code']})")

    if args.dry_run:
        log.info("\n--dry-run 모드 — 수집 생략")
        return

    # 분기 결정
    year, quarter = auto_detect_quarter()
    log.info(f"\n수집 분기: {year} {quarter}")

    confirm = input(f"\n{len(filtered)}개 종목 재무 수집 시작? (y/N): ")
    if confirm.lower() != 'y':
        log.info("취소")
        return

    corp_codes = filtered['corp_code'].unique().tolist()
    ok, fail = run_by_corp_codes(corp_codes, year, quarter)
    log.info(f"\n✅ 완료: 성공 {ok}개 / 실패 {fail}개")

    # app_config 저장
    corps_data = filtered[['corp_code','corp_name','report_nm']].to_dict('records')
    sb.table('app_config').upsert({
        'key': 'today_earnings_corps',
        'value': json.dumps(corps_data, ensure_ascii=False),
        'description': f'{target_date} 실적 공시 종목 목록'
    }, on_conflict='key').execute()
    log.info("✅ app_config 저장 완료")


if __name__ == '__main__':
    main()
