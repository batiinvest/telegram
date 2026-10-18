"""
scripts/regrade_q4_history.py
─────────────────────────────
지난 4분기 등급 이력(earnings_grade_history)에서 '틀린 4분기 영업이익률' 탓인 부분만 바로잡음 (2026-10-18)

4분기 영업이익률이 연간 값으로 저장돼 있었다(5605c6b에서 수정·재계산). 4분기 등급은 '전년 4분기 대비
영업이익률 +2%p'(S 조건)와 S 점수에 이익률을 쓰므로 S/A·점수·저장 이익률이 틀렸다.

이력에는 그 뒤 바뀐 데이터(재수집 등)와 05-03 일괄 입력분(점수 없음·변동 'maintain')이 섞여 있어서,
지금 데이터로 그냥 다시 매기면 이익률과 상관없는 변화까지 덮어쓴다. 그래서 종목마다 두 번 매긴다.
  ① 옛 이익률(backups/financials_q4_ratios_20261017_*.json, 연간 기준) ② 바로잡은 이익률
  ①이 저장 기록(등급·점수)을 그대로 재현하면 → ② 결과로 등급·점수를 바꾼다(차이 = 이익률 효과뿐).
  재현 못 하면(그 사이 다른 데이터가 바뀜) → 등급은 두고 저장 이익률만 바로잡는다.
- 등급이 바뀐 기록의 등급 변동(직전 3분기 기록 대비), 다음 해 1분기 기록의 등급 변동(4분기 대비)도 다시.
  일괄 입력분(점수 없음)은 변동 값이 자리표시라 손대지 않는다. 새 기록은 추가하지 않는다.
바꾸기 전 행은 backups/grade_history_q4_*.json.

실행: python3 scripts/regrade_q4_history.py --dry  /  python3 scripts/regrade_q4_history.py
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, '/home/kjhofone')
os.chdir('/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

import grade as G
from collect_financials import _get_sb
from db_utils import fetch_all_pages
from fin_rules import is_annual_q4

YEARS = ('2023', '2024', '2025')
T = 'earnings_grade_history'
SEL = ('id,stock_code,corp_name,bsns_year,quarter,revenue,operating_profit,operating_margin,'
       'revenue_yoy,op_profit_yoy,other_operating_income,revenue_qoq,is_cumulative')


def _change(cur, prev):
    if prev is None:
        return 'new'
    a, b = G.GRADE_RANK.get(cur, 0), G.GRADE_RANK.get(prev, 0)
    return 'up' if a > b else 'down' if a < b else 'maintain'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    sb = _get_sb()

    old_opm = {}                                   # financials id → 10-17 고치기 전 영업이익률
    for f in sorted(glob.glob('/home/kjhofone/backups/financials_q4_ratios_20261017_*.json')):
        for r in json.load(open(f)):
            old_opm.setdefault(r['id'], r['operating_margin'])

    def fin(y, q):
        rows = fetch_all_pages(sb.table('financials').select(SEL).eq('bsns_year', y).eq('quarter', q).eq('fs_div', 'CFS'))
        return {r['stock_code']: r for r in rows if not is_annual_q4(r, q)}

    def hist(y, q):
        return {r['stock_code']: r for r in sb.table(T).select('*').eq('bsns_year', y).eq('quarter', q).execute().data}

    def with_old(r):
        return dict(r, operating_margin=old_opm.get(r['id'], r['operating_margin'])) if r else r

    plan, backup, stat = [], [], {}
    for y in YEARS:
        cur, pq, pq2, py = fin(y, 'Q4'), fin(y, 'Q3'), fin(y, 'Q2'), fin(str(int(y) - 1), 'Q4')
        py_old = {c: with_old(r) for c, r in py.items()}
        q3_grade = {c: r['grade'] for c, r in hist(y, 'Q3').items()}
        st = stat.setdefault(y, {'기록': 0, '재현': 0, '등급 바뀜': 0, '점수 바뀜': 0, '이익률만': 0})
        q4_new_grade = {}
        for c, o in hist(y, 'Q4').items():
            st['기록'] += 1
            r = cur.get(c)
            upd = {}
            if r and str(r['operating_margin']) != str(o['operating_margin']):
                upd['operating_margin'] = r['operating_margin']
            g_old = G.grade_row(with_old(r), pq, py_old, pq2) if r else None
            g_new = G.grade_row(r, pq, py, pq2) if r else None
            seeded = o.get('score') is None
            same = g_old and g_old['grade'] == o['grade'] and (seeded or g_old['score'] == o['score'])
            if same and g_new:
                st['재현'] += 1
                if g_new['grade'] != o['grade']:
                    upd['grade'] = g_new['grade']
                    q4_new_grade[c] = g_new['grade']
                    st['등급 바뀜'] += 1
                    if not seeded:
                        upd['grade_change'] = _change(g_new['grade'], q3_grade.get(c))
                if not seeded and g_new['score'] != o['score']:
                    upd['score'] = g_new['score']
                    st['점수 바뀜'] += 1
            elif upd:
                st['이익률만'] += 1
            if upd:
                plan.append((y, 'Q4', c, o['corp_name'], upd))
                backup.append(o)
        ny = str(int(y) + 1)
        for c, o in hist(ny, 'Q1').items():
            if c in q4_new_grade and o.get('score') is not None:
                ch = _change(o['grade'], q4_new_grade[c])
                if ch != o['grade_change']:
                    plan.append((ny, 'Q1', c, o['corp_name'], {'grade_change': ch}))
                    backup.append(o)

    print('4분기 기록', stat)
    print('고칠 행', len(plan), '(1분기 변동', sum(1 for p in plan if p[1] == 'Q1'), ')')
    for y, q, c, nm, d in plan:
        if 'grade' in d or 'grade_change' in d:
            print(' ', y, q, nm, d)
    if a.dry or not plan:
        return

    os.makedirs('/home/kjhofone/backups', exist_ok=True)
    bpath = f"/home/kjhofone/backups/grade_history_q4_{datetime.now():%Y%m%d_%H%M%S}.json"
    json.dump(backup, open(bpath, 'w'), ensure_ascii=False, default=str)
    print('백업', len(backup), '행 →', bpath)
    for y, q, c, nm, d in plan:
        sb.table(T).update(d).eq('stock_code', c).eq('bsns_year', y).eq('quarter', q).execute()
    print('갱신', len(plan), '행')


if __name__ == '__main__':
    main()
