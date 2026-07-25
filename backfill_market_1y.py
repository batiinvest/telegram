# -*- coding: utf-8 -*-
"""
backfill_market_1y.py
모니터링 종목의 과거 시세를 약 400일(=252거래일+버퍼)까지 백필.
KIS inquire-daily-itemchartprice는 호출당 최대 100거래일 → 날짜 윈도 페이지네이션.
목적: market_data 이력을 늘려 collect_market의 year_return(252일) 계산 가능케 함.
원주가(FID_ORG_ADJ_PRC='1') — 시스템 관행과 통일. 기존 행은 건드리지 않음(누락일만 insert).

실행: cd /home/kjhofone && python3 backfill_market_1y.py [--code 005930] [--days 400]
"""
import os, sys, time, argparse
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')
sys.path.insert(0, os.path.dirname(__file__))
from logger_config import get_logger
log = get_logger(__name__)
from db_client import get_supabase_client
from stock_api import _call_kis_api
from db_utils import fetch_all_pages

def fetch_window(code, start_dt, end_dt):
    data = _call_kis_api(
        tr_id='FHKST03010100', path='quotations/inquire-daily-itemchartprice',
        code=code, custtype='P',
        extra_params={'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': code,
                      'FID_INPUT_DATE_1': start_dt, 'FID_INPUT_DATE_2': end_dt,
                      'FID_PERIOD_DIV_CODE': 'D', 'FID_ORG_ADJ_PRC': '1'})
    return data.get('output2', []) if data else []

def get_daily_year(code, target_days=400, max_windows=6):
    all_bars = {}
    end = datetime.now()
    target_str = (datetime.now() - timedelta(days=target_days)).strftime('%Y%m%d')
    for _ in range(max_windows):
        s = (end - timedelta(days=140)).strftime('%Y%m%d')
        e = end.strftime('%Y%m%d')
        bars = fetch_window(code, s, e)
        if not bars:
            break
        for b in bars:
            d = b.get('stck_bsop_date', '')
            if len(d) == 8:
                all_bars[d] = b
        oldest = min(b.get('stck_bsop_date', '99999999') for b in bars)
        if oldest <= target_str:
            break
        end = datetime.strptime(oldest, '%Y%m%d') - timedelta(days=1)
        time.sleep(0.15)
    return list(all_bars.values())

def build_rows(code, name, mkt, daily, existing_dates):
    rows = []
    for d in daily:
        ds = d.get('stck_bsop_date', '')
        if len(ds) != 8:
            continue
        date_fmt = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        if date_fmt in existing_dates:
            continue
        try:
            price = int(d.get('stck_clpr', 0)) or None
            prdy_vrss = int(d.get('prdy_vrss', 0))
            sign = d.get('prdy_vrss_sign', '3')
            prdy_vrss = -abs(prdy_vrss) if sign in ('4', '5') else abs(prdy_vrss)
            prev_price = (price or 0) - prdy_vrss
            price_chg = round(prdy_vrss / prev_price * 100, 2) if prev_price else 0.0
            mkt_cap = int(d.get('hts_avls', 0)) or None   # 억원 단위일 수 있음 -> 확인 필요
            vol = int(d.get('acml_vol', 0)) or None
        except (ValueError, TypeError):
            continue
        row = {'stock_code': code, 'corp_name': name, 'base_date': date_fmt,
               'price': price, 'price_change_rate': price_chg, 'volume': vol}
        if mkt:
            row['market'] = mkt
        if mkt_cap:
            row['market_cap'] = mkt_cap
        rows.append(row)
    return rows

def run(target_code=None, target_days=400):
    sb = get_supabase_client()
    q = sb.table('companies').select('code,name,market').eq('is_monitored', True)
    if target_code:
        q = sb.table('companies').select('code,name,market').eq('code', target_code)
    companies = q.execute().data or []
    log.info(f"[1y백필] 대상 {len(companies)}종목, target_days={target_days}")
    total_ok = 0
    for i, c in enumerate(companies):
        code = c['code'].split('.')[0]
        name = c.get('name', '')
        mkt = c.get('market')
        # 기존 날짜 집합
        ex = fetch_all_pages(sb.table('market_data').select('base_date').eq('stock_code', code))
        existing = {r['base_date'] for r in ex}
        daily = get_daily_year(code, target_days)
        rows = build_rows(code, name, mkt, daily, existing)
        for j in range(0, len(rows), 50):
            clean = [{k: v for k, v in r.items() if v is not None} for r in rows[j:j+50]]
            sb.table('market_data').upsert(clean, on_conflict='stock_code,base_date').execute()
        total_ok += len(rows)
        if (i + 1) % 20 == 0 or target_code:
            log.info(f"  [{i+1}/{len(companies)}] {name}({code}): +{len(rows)}건 (기존 {len(existing)})")
        time.sleep(0.15)
    log.info(f"[1y백필] 완료: 총 {total_ok}건 신규 저장")
    return total_ok

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--code', default=None)
    ap.add_argument('--days', type=int, default=400)
    a = ap.parse_args()
    run(a.code, a.days)
