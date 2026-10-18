"""
scripts/fill_missing_growth.py
──────────────────────────────
저장 성장률(YoY·QoQ) 빈칸 채우기 (2026-10-18)

공시 기반 당일 수집(run_by_corp_codes)이 비교용 전년동기·직전 분기 행을 그날 종목 전부 한 번에 조회해
1,000행에서 잘렸다 → 마감일에 잘린 종목의 매출·영업이익·순이익 YoY/QoQ가 빈칸으로 저장됐다
(2026Q2 매출 YoY 271·QoQ 434행 등). 수집기는 같은 날 고쳤고, 이 스크립트는 저장된 행 전부에
수집기와 같은 calculate_growth_rates(전체 캐시)를 돌려 '저장값이 빈칸인데 계산되는' 칸만 채운다.
값이 이미 있는 칸은 건드리지 않는다(다른 행 수 = 참고 출력). 바꾸기 전 값은 backups/financials_growth_fill_*.json.

실행: python3 scripts/fill_missing_growth.py --dry  /  python3 scripts/fill_missing_growth.py
"""
import argparse
import collections
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, '/home/kjhofone')
os.chdir('/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

from collect_financials import _get_sb, build_fin_cache, calculate_growth_rates
from db_utils import fetch_all_pages

G = ('revenue_yoy', 'revenue_qoq', 'op_profit_yoy', 'op_profit_qoq', 'net_income_yoy', 'net_income_qoq')
SEL = 'id,stock_code,corp_name,bsns_year,quarter,fs_div,is_cumulative,revenue,operating_profit,net_income,' + ','.join(G)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    sb = _get_sb()
    cache = build_fin_cache(sb)
    rows = fetch_all_pages(sb.table('financials').select(SEL).order('id'))
    print(f"행 {len(rows):,} · 캐시 {sum(len(v) for v in cache.values()):,}")

    plan, differ, by_q, by_col = [], 0, collections.Counter(), collections.Counter()
    for r in rows:
        g = calculate_growth_rates(cache, r)
        fill = {k: v for k, v in g.items() if r.get(k) is None and v is not None}
        differ += sum(1 for k, v in g.items() if r.get(k) is not None and v is not None and abs(float(r[k]) - v) > 0.011)
        if fill:
            plan.append((r, fill))
            by_q[f"{r['bsns_year']}{r['quarter']}"] += 1
            by_col.update(fill.keys())
    print('채울 행', len(plan), '· 분기별', dict(sorted(by_q.items())))
    print('  칸별', dict(by_col))
    print('  (참고) 값이 있는데 지금 계산과 다른 칸', differ)
    for r, f in plan[:4]:
        print('  예)', r['corp_name'], r['bsns_year'], r['quarter'], r['fs_div'], f)
    if a.dry or not plan:
        return

    os.makedirs('/home/kjhofone/backups', exist_ok=True)
    bpath = f"/home/kjhofone/backups/financials_growth_fill_{datetime.now():%Y%m%d_%H%M%S}.json"
    json.dump([{'id': r['id'], **{k: r[k] for k in G}} for r, _ in plan], open(bpath, 'w'), default=str)
    print('백업', len(plan), '행 →', bpath)

    def upd(t):
        r, f = t
        for _ in range(3):
            try:
                sb.table('financials').update(f).eq('id', r['id']).execute()
                return True
            except Exception as e:
                err = e
        print('  실패', r['corp_name'], r['bsns_year'], r['quarter'], str(err)[:100])
        return False

    with ThreadPoolExecutor(4) as ex:
        ok = sum(ex.map(upd, plan))
    print(f"갱신 {ok}/{len(plan)}행")


if __name__ == '__main__':
    main()
