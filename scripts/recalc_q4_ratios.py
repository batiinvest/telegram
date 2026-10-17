"""
scripts/recalc_q4_ratios.py
───────────────────────────
저장된 4분기 행의 손익 비율을 4분기 단독 금액으로 다시 계산 (2026-10-17)

수집기는 calc_ratios를 사업보고서 '연간' 행에서 계산한 뒤 convert_to_pure_quarter가 금액만 4분기 단독
(연간 − 3분기 누적)으로 바꾸고 비율은 그대로 뒀다 → 4분기 영업이익률·순이익률·매출원가율·GPM·판관비율·
ROE·ROA가 연간 값이었다(2023~25 4분기 행의 ~89%). 수집기는 같은 날 고쳤고(_flow_ratios), 이 스크립트는
이미 저장된 행에 같은 식을 적용한다. DART 호출 없음.

대상 = 4분기 · 연간 행 아님(fin_rules.is_annual_q4 — 연간 행은 금액도 연간이라 비율이 맞다).
바뀌는 칸만 행 단위 UPDATE, 바꾸기 전 값은 backups/financials_q4_ratios_*.json.

실행:
  python3 scripts/recalc_q4_ratios.py --dry     # 바뀔 행 수·예시만
  python3 scripts/recalc_q4_ratios.py
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, '/home/kjhofone')
os.chdir('/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

from collect_financials import FLOW_RATIO_COLS, _flow_ratios, _get_sb
from fin_rules import is_annual_q4

AMOUNT_COLS = ('revenue', 'cogs', 'gross_profit', 'sga', 'operating_profit', 'net_income',
               'total_equity', 'total_assets')
SEL = ','.join(('id', 'stock_code', 'corp_name', 'bsns_year', 'quarter', 'fs_div', 'is_cumulative')
               + AMOUNT_COLS + FLOW_RATIO_COLS)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 0.006


def _fetch(sb, quarter):
    rows, off = [], 0
    while True:
        d = sb.table('financials').select(SEL).eq('quarter', quarter).order('id') \
            .range(off, off + 999).execute().data
        rows += d
        off += 1000
        if len(d) < 1000:
            return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    sb = _get_sb()

    # 1~3분기는 원래 맞아야 한다 — 같은 식이 저장값을 재현하는지 먼저 확인(식이 틀렸으면 멈춤)
    chk = [r for r in _fetch(sb, 'Q3') if not r.get('is_cumulative')]
    off = sum(1 for r in chk if any(not _same(r[k], v) for k, v in _flow_ratios(r).items()))
    print(f"[점검] 3분기 {len(chk)}행 중 식이 저장값과 다른 행 {off}개")
    if off > len(chk) * 0.01:
        sys.exit('3분기 저장값을 재현하지 못함 — 식 확인 필요, 중단')

    rows = _fetch(sb, 'Q4')
    targets, annual, by_year, by_col = [], 0, {}, {}
    for r in rows:
        if is_annual_q4(r):
            annual += 1
            continue
        new = _flow_ratios(r)
        diff = {k: v for k, v in new.items() if not _same(r[k], v)}
        if diff:
            targets.append((r, diff))
            by_year[r['bsns_year']] = by_year.get(r['bsns_year'], 0) + 1
            for k in diff:
                by_col[k] = by_col.get(k, 0) + 1
    print(f"4분기 {len(rows)}행 · 연간 행(제외) {annual} · 바뀔 행 {len(targets)}")
    print('  연도별', dict(sorted(by_year.items())))
    print('  칸별', by_col)
    for r, d in targets[:5]:
        print('  예)', r['corp_name'], r['bsns_year'], r['fs_div'],
              {k: (r[k], v) for k, v in d.items() if k in ('operating_margin', 'net_margin', 'roe')})
    if a.dry or not targets:
        return

    os.makedirs('/home/kjhofone/backups', exist_ok=True)
    bpath = f"/home/kjhofone/backups/financials_q4_ratios_{datetime.now():%Y%m%d_%H%M%S}.json"
    json.dump([{'id': r['id'], **{k: r[k] for k in FLOW_RATIO_COLS}} for r, _ in targets],
              open(bpath, 'w'), ensure_ascii=False, default=str)
    print(f"백업 {len(targets)}행 → {bpath}")

    def upd(t):
        r, d = t
        for _ in range(3):
            try:
                sb.table('financials').update(d).eq('id', r['id']).execute()
                return True
            except Exception as e:
                err = e
        print('  실패', r['corp_name'], r['bsns_year'], str(err)[:120])
        return False

    with ThreadPoolExecutor(8) as ex:
        ok = sum(ex.map(upd, targets))
    print(f"갱신 {ok}/{len(targets)}행")


if __name__ == '__main__':
    main()
