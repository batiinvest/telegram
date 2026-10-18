"""지분공시 insider_summary 다시 계산 — 보고서(rcept_no) 한 건 기준으로 (2026-10-18).

예전 수집(jobs_collect._preprocess_disclosures)은 지분공시마다 '그 회사의 그날 전체 매매 합계'를 붙였다
(삼성전자 07-21은 761건 행이 모두 같은 합계). 지금은 그 보고서의 insider_trades 행만 → insider_report_summary.
insider_trades에 그 보고서가 없으면 요약을 비운다(그날 합계를 남기면 또 잘못 읽힘).

  python3 scripts/backfill_insider_summary.py          # 바뀔 건수만
  python3 scripts/backfill_insider_summary.py --apply  # 백업 후 반영
"""
import os, sys, json, argparse
from datetime import datetime

sys.path.insert(0, '/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')
from supabase import create_client
from db_utils import fetch_all_pages
from jobs_collect import insider_report_summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    sb = create_client(os.environ['SB_URL'], os.environ['SB_SERVICE_KEY'])

    disc = fetch_all_pages(sb.table('daily_disclosures').select('rcept_no,base_date,corp_code,insider_summary')
                           .eq('category', '지분공시').order('rcept_no'))
    rnos = [d['rcept_no'] for d in disc if d.get('rcept_no')]
    trades = {}
    for i in range(0, len(rnos), 150):
        for r in fetch_all_pages(sb.table('insider_trades').select('id,rcept_no,reporter,shares_change')
                                 .in_('rcept_no', rnos[i:i + 150]).order('id')):
            trades.setdefault(r['rcept_no'], []).append(r)

    changes = []
    for d in disc:
        new = insider_report_summary(trades[d['rcept_no']]) if d['rcept_no'] in trades else None
        if new != d.get('insider_summary'):
            changes.append((d, new))
    filled = sum(1 for _, n in changes if n)
    cleared = sum(1 for _, n in changes if n is None)
    print(f"지분공시 {len(disc)}행 · insider_trades 짝 {len(trades)}건 · 바뀜 {len(changes)}행 (보고서 기준 {filled} · 짝 없어 비움 {cleared})")
    for d, n in changes[:5]:
        print('  ', d['base_date'], d['rcept_no'], d.get('insider_summary'), '→', n)
    if not a.apply:
        return
    os.makedirs('/home/kjhofone/backups', exist_ok=True)
    bpath = f"/home/kjhofone/backups/insider_summary_{datetime.now():%Y%m%d_%H%M%S}.json"
    json.dump([{'rcept_no': d['rcept_no'], 'insider_summary': d.get('insider_summary')} for d, _ in changes],
              open(bpath, 'w'), ensure_ascii=False)
    print('백업', bpath)
    done = 0
    for d, n in changes:
        sb.table('daily_disclosures').update({'insider_summary': n}).eq('rcept_no', d['rcept_no']).execute()
        done += 1
        if done % 500 == 0:
            print('  ', done)
    print('반영', done)


if __name__ == '__main__':
    main()
