
from logger_config import get_logger
log = get_logger(__name__)
"""
backfill_market_90d.py
모니터링 종목 과거 90일 시장 데이터(일별 등락률) 백필

서버 실행:
  cd /home/kjhofone && python3 backfill_market_90d.py
"""

import os, sys, time, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from supabase_bridge import bridge
from stock_api import _call_kis_api

def get_daily_prices(code: str, days: int = 95) -> list:
    """KIS API로 과거 N일 일별 시가/종가/등락률 조회"""
    end_dt   = datetime.now().strftime('%Y%m%d')
    start_dt = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
    try:
        data = _call_kis_api(
            tr_id='FHKST03010100',
            path='quotations/inquire-daily-itemchartprice',
            code=code, custtype='P',
            extra_params={
                'FID_INPUT_DATE_1': start_dt,
                'FID_INPUT_DATE_2': end_dt,
                'FID_PERIOD_DIV_CODE': 'D',
                'FID_ORG_ADJ_PRC': '1',
            }
        )
        return data.get('output2', []) if data else []
    except Exception as e:
        logging.error(f"  [{code}] API 오류: {e}")
        return []


def run(force=False):
    sb = bridge._get_client()

    # 모니터링 종목 목록
    res = sb.table('companies').select('code,name,industry,sub_industry') \
            .eq('is_monitored', True).execute()
    companies = res.data or []
    logging.info(f"모니터링 종목: {len(companies)}개 (force={force})")

    total_ok = 0
    total_skip = 0

    for i, c in enumerate(companies):
        code  = c['code'].split('.')[0]
        name  = c.get('name', '')
        mkt   = 'KOSPI' if c['code'].endswith('.KS') else 'KOSDAQ'

        # 이미 저장된 날짜 확인 (force 모드에서는 스킵 없음)
        if not force:
            existing = sb.table('market_data').select('base_date') \
                         .eq('stock_code', code) \
                         .gte('base_date', (datetime.now() - timedelta(days=95)).strftime('%Y-%m-%d')) \
                         .execute()
            existing_dates = {r['base_date'] for r in (existing.data or [])}
        else:
            existing_dates = set()  # force 모드: 모두 덮어쓰기

        # KIS API 일별 데이터 조회
        daily = get_daily_prices(code)
        if not daily:
            logging.warning(f"  [{i+1}/{len(companies)}] {name}({code}): 데이터 없음")
            time.sleep(0.1)
            continue

        rows = []
        for d in daily:
            date_str = d.get('stck_bsop_date', '')
            if not date_str or len(date_str) != 8:
                continue
            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            if date_fmt in existing_dates:
                total_skip += 1
                continue

            try:
                price     = int(d.get('stck_clpr', 0)) or None
                prdy_vrss = int(d.get('prdy_vrss', 0))
                sign      = d.get('prdy_vrss_sign', '3')
                if sign in ('4', '5'):
                    prdy_vrss = -abs(prdy_vrss)
                else:
                    prdy_vrss = abs(prdy_vrss)
                prev_price = (price or 0) - prdy_vrss
                price_chg  = round(prdy_vrss / prev_price * 100, 2) if prev_price else 0.0
                mkt_cap   = int(d.get('hts_avls', 0)) or None
                vol       = int(d.get('acml_vol', 0)) or None
            except (ValueError, TypeError):
                continue

            rows.append({
                'stock_code':        code,
                'corp_name':         name,
                'base_date':         date_fmt,
                'market':            mkt,
                'price':             price,
                'price_change_rate': price_chg,
                'market_cap':        mkt_cap,
                'volume':            vol,
            })

        if rows:
            for j in range(0, len(rows), 50):
                # None 값은 제외하고 upsert → 기존 DB 값 보존
                clean_rows = []
                for row in rows[j:j+50]:
                    clean_rows.append({k: v for k, v in row.items() if v is not None})
                sb.table('market_data').upsert(
                    clean_rows, on_conflict='stock_code,base_date'
                ).execute()
            total_ok += len(rows)

        logging.info(f"  [{i+1}/{len(companies)}] {name}({code}): {len(rows)}건 저장, {total_skip}건 스킵")
        time.sleep(0.15)

    logging.info(f"\n✅ 완료: 총 {total_ok}건 저장, {total_skip}건 스킵")


def run_for_codes(codes: list, force: bool = False):
    """
    신규 등록 종목 등 특정 종목코드만 90일 백필.
    run_all.py reload 시 신규 추가 종목 처리에 사용.
    (run_all.py 인라인 _backfill_market 함수 중복 제거)
    """
    sb = bridge._get_client()

    # 종목 정보 조회 (code 접미사 제거)
    codes_clean = [c.split('.')[0] for c in codes]
    res = sb.table('companies').select('code,name') \
            .in_('code', codes).execute()
    comp_map = {}
    for r in (res.data or []):
        c   = r['code'].split('.')[0]
        mkt = 'KOSPI' if r['code'].endswith('.KS') else 'KOSDAQ'
        comp_map[c] = {'name': r.get('name', ''), 'market': mkt}

    total_ok = 0
    for code in codes_clean:
        info   = comp_map.get(code, {'name': '', 'market': 'KOSDAQ'})
        daily  = get_daily_prices(code)
        if not daily:
            logging.warning(f"  [{code}] 데이터 없음")
            time.sleep(0.1)
            continue

        if not force:
            existing = sb.table('market_data').select('base_date') \
                         .eq('stock_code', code) \
                         .gte('base_date', (datetime.now() - timedelta(days=95)).strftime('%Y-%m-%d')) \
                         .execute()
            existing_dates = {r['base_date'] for r in (existing.data or [])}
        else:
            existing_dates = set()

        rows = []
        for d in daily:
            date_str = d.get('stck_bsop_date', '')
            if not date_str or len(date_str) != 8: continue
            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            if date_fmt in existing_dates: continue
            try:
                price      = int(d.get('stck_clpr', 0)) or None
                prdy_vrss  = int(d.get('prdy_vrss', 0))
                sign       = d.get('prdy_vrss_sign', '3')
                prdy_vrss  = -abs(prdy_vrss) if sign in ('4','5') else abs(prdy_vrss)
                prev_price = (price or 0) - prdy_vrss
                price_chg  = round(prdy_vrss / prev_price * 100, 2) if prev_price else 0.0
                rows.append({
                    'stock_code':        code,
                    'corp_name':         info['name'],
                    'base_date':         date_fmt,
                    'market':            info['market'],
                    'price':             price,
                    'price_change_rate': price_chg,
                    'market_cap':        int(d.get('hts_avls', 0)) or None,
                    'volume':            int(d.get('acml_vol', 0)) or None,
                })
            except (ValueError, TypeError): continue

        for j in range(0, len(rows), 50):
            sb.table('market_data').upsert(
                rows[j:j+50], on_conflict='stock_code,base_date'
            ).execute()
        total_ok += len(rows)
        logging.info(f"  [{code}] {info['name']}: {len(rows)}건 저장")
        time.sleep(0.15)

    logging.info(f"📈 [백필] 완료: 총 {total_ok}건")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='기존 데이터도 덮어쓰기')
    args = parser.parse_args()
    run(force=args.force)
