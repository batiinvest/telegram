"""
scripts/fill_bs_multi.py
────────────────────────
재무상태표 유동·비유동 자산·부채 빈칸 채우기 — DART 다중회사 주요계정(fnlttMultiAcnt, 한 번에 100곳) (2026-10-12)

2026년 2분기(반기보고서)부터 DART가 계정명을 '유동 자산'·'비유동 부채'처럼 띄어 써서 수집기가 못 읽었다
(2026 2분기 current_assets 채움률 11%, 그전 분기 96%). 수집기는 이름 정규화(resolve_account 3단계)로 고쳤고,
이미 저장된 행은 이 스크립트로 채운다 — 종목마다 finstate_all 1~2건 대신 100곳에 1건.
  - 대상: 그 분기 행 중 네 칸 가운데 하나라도 빈 행. 원화 공시만(외화 공시는 다시 받기로 원화 환산).
  - 채우는 칸: current_assets·non_current_assets·current_liabilities·non_current_liabilities 중 빈 칸 + current_ratio
  - 같은 fs_div(CFS/OFS) 값만 쓰고, 유동+비유동 자산이 저장된 자산총계와 1% 넘게 다르면 건너뛴다.

실행:
  python3 scripts/fill_bs_multi.py --year 2026 --quarter Q2 --dry
  python3 scripts/fill_bs_multi.py --year 2026 --quarter Q2
"""
import argparse
import os
import sys
import time

sys.path.insert(0, '/home/kjhofone')
os.chdir('/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

from OpenDartReader import dart_finstate
from db_client import get_supabase_client
from collect_financials import resolve_account, parse_amount, _clamp_ratio, QUARTER_REPORT

COLS = ('current_assets', 'non_current_assets', 'current_liabilities', 'non_current_liabilities')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--year', required=True)
    ap.add_argument('--quarter', required=True)
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    y, q = a.year, a.quarter.upper()
    sb = get_supabase_client()

    rows, page = [], 0
    sel = 'stock_code,corp_code,corp_name,fs_div,total_assets,' + ','.join(COLS)
    while True:
        r = sb.table('financials').select(sel).eq('bsns_year', y).eq('quarter', q) \
            .range(page * 1000, page * 1000 + 999).execute().data
        rows += r
        if len(r) < 1000:
            break
        page += 1
    todo = [r for r in rows if r.get('corp_code') and any(r.get(c) is None for c in COLS)]
    print(f'{y} {q}: {len(rows)}행 중 빈 칸 있는 행 {len(todo)}')
    corps = sorted({r['corp_code'] for r in todo})

    got, calls = {}, 0      # (corp_code, fs_div) → {col: 금액}
    non_krw = set()
    for i in range(0, len(corps), 100):
        calls += 1
        df = dart_finstate.finstate(os.getenv('DART_API_KEY'), ','.join(corps[i:i + 100]), y, QUARTER_REPORT[q][0])
        if df is None or df.empty:
            continue
        for _, x in df.iterrows():
            if str(x.get('currency') or 'KRW').upper() != 'KRW':
                non_krw.add(x['corp_code'])
                continue
            if x.get('sj_div') != 'BS':
                continue
            col = resolve_account(str(x.get('account_nm', '')).strip())
            amt = parse_amount(str(x.get('thstrm_amount', '')))
            if col in COLS and amt is not None:
                got.setdefault((x['corp_code'], x['fs_div']), {}).setdefault(col, amt)
        time.sleep(0.2)
    print(f'DART {calls}건 · 받은 (회사, 연결/별도) {len(got)} · 외화 공시 건너뜀 {len(non_krw)}')

    upd = skip_mis = skip_none = 0
    for r in todo:
        v = got.get((r['corp_code'], r['fs_div']))
        if not v or r['corp_code'] in non_krw:
            skip_none += 1
            continue
        ta = r.get('total_assets')
        ca, nca = v.get('current_assets'), v.get('non_current_assets')
        if ta and ca is not None and nca is not None and abs(ca + nca - ta) > abs(ta) * 0.01:
            skip_mis += 1
            print(f"  자산 불일치 건너뜀 {r['stock_code']} {r['corp_name']} {r['fs_div']}: {ca}+{nca} vs {ta}")
            continue
        new = {c: v[c] for c in COLS if r.get(c) is None and v.get(c) is not None}
        if not new:
            skip_none += 1
            continue
        ca2 = r.get('current_assets') if r.get('current_assets') is not None else new.get('current_assets')
        cl2 = r.get('current_liabilities') if r.get('current_liabilities') is not None else new.get('current_liabilities')
        if ca2 and cl2:
            new['current_ratio'] = _clamp_ratio(round(ca2 / cl2 * 100, 2))
        upd += 1
        if not a.dry:
            sb.table('financials').update(new).eq('stock_code', r['stock_code']).eq('bsns_year', y) \
                .eq('quarter', q).eq('fs_div', r['fs_div']).execute()
    print(f"{'(dry) ' if a.dry else ''}채움 {upd}행 · 값 없음 {skip_none} · 자산 불일치 {skip_mis}")


if __name__ == '__main__':
    main()
