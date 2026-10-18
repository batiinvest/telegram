"""지분 변동 이력 채우기 — DART 목록 API로 insider_trades에 없는 보고서를 넣는다 (2026-10-18).

  majorstock = 대량보유 상황보고(보고자·약식/일반·보유 주식·증감·비율·사유)
  elestock   = 임원·주요주주 소유보고(보고자·직위·보유·증감)
쓰임: ① 공시 수집(2026-05-12~) 이전 약 2년 이력(종목리포트 지분 변동내역이 insider_trades로 보여 줌)
     ② 수집 기간 중 본문(XML)을 못 읽어 빠진 보고서(대량보유 '일반' — 원익홀딩스 05-15 등)
목록 API에는 사유(장내매수 등)·단가·실제 매매일이 없다 → trade_date = 접수일, reason_code = majorstock/elestock.
이미 insider_trades에 있는 보고서(rcept_no)는 건드리지 않는다(본문에서 읽은 행이 더 자세함).

  python3 scripts/backfill_ownership_history.py --codes 240810,005930 --dry
  python3 scripts/backfill_ownership_history.py                       # 전 종목, 2024-05-01 ~ 어제
"""
import os, sys, time, argparse
from datetime import date, timedelta

sys.path.insert(0, '/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')
import requests
from supabase import create_client
from db_utils import fetch_all_pages
from collect_utils import batch_upsert
from collect_insider import row_from_majorstock, row_from_elestock

CONV = {'majorstock': row_from_majorstock, 'elestock': row_from_elestock}


def fetch_list(kind, corp_code, key):
    """(목록, 상태) — 상태 020(요청 한도 초과)·그 밖 오류는 호출부가 멈춘다. 013 = 자료 없음."""
    for attempt in range(3):
        try:
            j = requests.get(f'https://opendart.fss.or.kr/api/{kind}.json',
                             params={'crtfc_key': key, 'corp_code': corp_code}, timeout=25).json()
            return j.get('list', []) if j.get('status') == '000' else [], j.get('status')
        except Exception as e:
            err = str(e)
            time.sleep(2 + attempt * 3)
    return [], f'ERR {err[:60]}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', help='종목코드 쉼표 목록(없으면 전 종목)')
    ap.add_argument('--from', dest='frm', default='2024-05-01')
    ap.add_argument('--to', default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument('--max-calls', type=int, default=6000)
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    sb = create_client(os.environ['SB_URL'], os.environ['SB_SERVICE_KEY'])
    key = os.environ['DART_API_KEY']

    comps = fetch_all_pages(sb.table('companies').select('code,name,corp_code').eq('active', True).order('code'))
    comps = [c for c in comps if c.get('corp_code') and c.get('code')]
    if a.codes:
        want = set(a.codes.split(','))
        comps = [c for c in comps if c['code'] in want]
    have = {r['rcept_no'] for r in fetch_all_pages(sb.table('insider_trades').select('rcept_no').order('id'))}
    print(f"대상 {len(comps)}종목 · 기간 {a.frm} ~ {a.to} · 이미 있는 보고서 {len(have):,}건", flush=True)

    calls, pending, seen = 0, [], set()
    added = {'bulk': 0, 'insider': 0}
    saved = 0
    stop = None
    for i, c in enumerate(comps, 1):
        if calls + 2 > a.max_calls:
            stop = f'호출 한도 {a.max_calls}'
            break
        for kind in ('majorstock', 'elestock'):
            items, st = fetch_list(kind, c['corp_code'], key)
            calls += 1
            if st not in ('000', '013'):
                stop = f'{c["code"]} {kind} 상태 {st}'
                break
            for it in items:
                d = (it.get('rcept_dt') or '')[:10]
                if not (a.frm <= d <= a.to) or not it.get('rcept_no') or it['rcept_no'] in have:
                    continue
                row = CONV[kind](it, c['code'])
                row['corp_name'] = row['corp_name'] or c['name']
                k = (row['rcept_no'], row['reporter'], row['trade_date'], row['stock_type'])
                if k in seen:
                    continue
                seen.add(k)
                pending.append(row)
                added[row['report_type']] += 1
            time.sleep(0.12)
        if stop:
            break
        if len(pending) >= 500 and not a.dry:
            saved += batch_upsert(sb, 'insider_trades', pending, 'rcept_no,reporter,trade_date,stock_type', chunk=250)
            pending = []
        if i % 100 == 0:
            print(f"  {i}/{len(comps)} · 호출 {calls} · 추가 대량보유 {added['bulk']:,} · 임원 {added['insider']:,}", flush=True)
    if a.dry:
        print('샘플:', pending[:3])
    elif pending:
        saved += batch_upsert(sb, 'insider_trades', pending, 'rcept_no,reporter,trade_date,stock_type', chunk=250)
    print(f"끝 · 호출 {calls} · 추가 대량보유 {added['bulk']:,} · 임원 {added['insider']:,} · 저장 {saved:,}"
          + (f" · 멈춘 이유: {stop}" if stop else ''), flush=True)


if __name__ == '__main__':
    main()
