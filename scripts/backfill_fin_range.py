"""
scripts/backfill_fin_range.py
─────────────────────────────
financials 과거 연도 백필 — DART 호출 예산을 지키며 여러 밤에 나눠 돈다 (2026-10-12, 실적 국면용 2021~2022)

backfill_financials.py의 수집·변환(_collect_and_save)을 그대로 쓰고, 그 위에 세 가지만 더한다.
  1) 연도 범위: --from-year ~ --to-year 분기만 (기본 스크립트는 from_year ~ 현재 분기 전부)
  2) DART 호출 예산: 한 번 돌 때 opendart 요청을 --max-calls 건까지만. 공시 봇·재무수집과 같은 키라
     하루 한도(키당 2만 건)를 넘기면 그날 실시간 공시 알림이 멈춘다. 한 쌍에 1~3건
     (연결 없으면 별도로 한 번 더, 4분기는 3분기 누적 한 번 더).
     응답 status '020'(사용한도 초과)이 오면 즉시 멈춘다 — 기본 스크립트는 이걸 '데이터 없음'으로 넘긴다.
  3) 상태 파일: DART에 없는 쌍(그 연도 미상장 등)을 기록해 다음 밤에 다시 부르지 않는다.
     남은 쌍이 없으면 crontab에서 자기 줄을 지운다.

실행:
  python3 scripts/backfill_fin_range.py --from-year 2021 --to-year 2022 --max-calls 9000
  python3 scripts/backfill_fin_range.py --from-year 2021 --to-year 2022 --dry    # 남은 쌍만 세기
  python3 scripts/backfill_fin_range.py --refetch-q4 data/q4_refetch.json --max-calls 3000
      # 4분기 행 다시 받기(백업 → 재수집 → 다음 해 1·4분기 성장률 갱신) — refetch_q4 참고
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, '/home/kjhofone')
os.chdir('/home/kjhofone')
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

import requests

import backfill_financials as bf
from collect_financials import build_fin_cache

STATE_DIR = '/home/kjhofone/data'
CRON_TAG = 'backfill_fin_range.py'


class BudgetExceeded(BaseException):
    """collect_one이 finstate_all 호출을 `except Exception`으로 감싸므로 BaseException이어야 빠져나온다
    (Exception이면 '데이터 없음'으로 삼켜져 남은 쌍이 전부 없음으로 기록된다)."""


_calls = {'n': 0, 'max': 0, 'quota': False}
_orig_get = requests.get


def _counted_get(url, *a, **kw):
    """opendart 요청만 센다. 예산을 넘기면 요청 전에 멈추고, 020이 오면 그 자리에서 멈춘다."""
    if 'opendart.fss.or.kr' in str(url):
        if _calls['n'] >= _calls['max']:
            raise BudgetExceeded('budget')
        _calls['n'] += 1
        r = _orig_get(url, *a, **kw)
        try:
            if str(url).endswith('.json') and r.json().get('status') == '020':
                _calls['quota'] = True
                raise BudgetExceeded('quota 020')
        except BudgetExceeded:
            raise
        except Exception:
            pass
        return r
    return _orig_get(url, *a, **kw)


def _log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _remove_cron():
    try:
        cur = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
        if CRON_TAG not in cur:
            return
        new = ''.join(l + '\n' for l in cur.splitlines() if CRON_TAG not in l)
        subprocess.run(['crontab', '-'], input=new, text=True, check=True)
        _log('crontab에서 백필 줄 제거')
    except Exception as e:
        _log(f'crontab 정리 실패: {e}')


def refetch_q4(pairs_file, max_calls):
    """4분기 행 다시 받기 — [[종목코드, 연도], ...] 목록. 2026-10-12 get_q3_cumulative 매출 계정 버그
    (4분기 = 연간 − 세부항목) 수정 뒤, 원본 DataGuide 분기 매출과 5% 넘게 다른 2023~2025 4분기 행을 고친다.
    덮어쓰기 전 원래 행을 backups/에 저장하고, 고친 4분기를 비교 기준으로 쓰는 다음 해 1분기(QoQ)·
    4분기(YoY) 행의 저장 성장률을 다시 계산한다."""
    from collect_financials import calculate_growth_rates
    pairs = sorted({(c, str(y)) for c, y in json.load(open(pairs_file))}, key=lambda p: (p[1], p[0]))
    sb = bf._get_sb()
    _calls['max'] = 10 ** 9
    comp = {t['stock_code']: t for t in bf._load_companies(sb)}
    backup = []
    by_year = {}
    for c, y in pairs:
        by_year.setdefault(y, []).append(c)
    for y, codes in by_year.items():
        for i in range(0, len(codes), 100):
            backup += sb.table('financials').select('*').in_('stock_code', codes[i:i + 100]) \
                .eq('bsns_year', y).eq('quarter', 'Q4').execute().data
    os.makedirs('/home/kjhofone/backups', exist_ok=True)
    bpath = f"/home/kjhofone/backups/financials_q4_refetch_{datetime.now():%Y%m%d_%H%M%S}.json"
    json.dump(backup, open(bpath, 'w'), ensure_ascii=False, default=str)
    _log(f"4분기 다시 받기 {len(pairs)}쌍 · 원래 행 {len(backup)}개 백업 → {bpath}")

    dart = bf.OpenDartReader(bf.DART_API_KEY)
    fin_cache = build_fin_cache(sb)
    requests.get = _counted_get
    _calls['n'], _calls['max'] = 0, max_calls
    done, stop = [], None
    for c, y in pairs:
        t = comp.get(c)
        if not t:
            continue
        try:
            if bf._collect_and_save(dart, sb, fin_cache, t, y, 'Q4'):
                done.append((c, y))
        except BudgetExceeded as e:
            stop = str(e)
            break
        except Exception as e:
            _log(f"  실패 {t['corp_name']} {y} Q4: {str(e)[:160]}")
        time.sleep(0.12)
    requests.get = _orig_get
    _log(f"다시 받음 {len(done)}/{len(pairs)} · DART {_calls['n']}건{' · 멈춘 이유: ' + stop if stop else ''}")

    # 이웃 행 성장률 — 다음 해 1분기 QoQ(직전 = 고친 4분기) · 다음 해 4분기 YoY(전년 = 고친 4분기)
    sel = 'stock_code,bsns_year,quarter,fs_div,is_cumulative,revenue,operating_profit,net_income'
    n = 0
    for c, y in done:
        ny = str(int(y) + 1)
        for r in sb.table('financials').select(sel).eq('stock_code', c).eq('bsns_year', ny) \
                .in_('quarter', ['Q1', 'Q4']).execute().data:
            g = calculate_growth_rates(fin_cache, r)
            sb.table('financials').update(g).eq('stock_code', c).eq('bsns_year', ny) \
                .eq('quarter', r['quarter']).eq('fs_div', r['fs_div']).execute()
            n += 1
    _log(f"이웃 행 성장률 다시 계산 {n}행")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-year', type=int)
    ap.add_argument('--to-year', type=int)
    ap.add_argument('--max-calls', type=int, default=9000)
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--refetch-q4', help='[[종목코드, 연도], ...] JSON 파일 — 4분기 행 다시 받기')
    a = ap.parse_args()
    if a.refetch_q4:
        refetch_q4(a.refetch_q4, a.max_calls)
        return

    state_path = f"{STATE_DIR}/backfill_fin_{a.from_year}_{a.to_year}.json"
    os.makedirs(STATE_DIR, exist_ok=True)
    state = json.load(open(state_path)) if os.path.exists(state_path) else {'nodata': [], 'runs': []}
    nodata = set(tuple(x) for x in state['nodata'])

    sb = bf._get_sb()
    quarters = [(str(y), q) for y in range(a.from_year, a.to_year + 1) for q in ('Q1', 'Q2', 'Q3', 'Q4')]
    _calls['max'] = 10 ** 9          # 목록 로딩(corpCode.xml 등)은 예산 밖
    companies = bf._load_companies(sb)
    existing = bf._load_existing(sb)
    missing = [(t, y, q) for t in companies for (y, q) in quarters
               if (t['stock_code'], y, q) not in existing and (t['stock_code'], y, q) not in nodata]
    _log(f"대상 {len(companies)}종목 × {len(quarters)}분기 · 이미 있음/DART 없음 제외 → 남은 쌍 {len(missing)}")
    if a.dry:
        return
    if not missing:
        _log('남은 쌍 없음 — 백필 완료')
        _remove_cron()
        return

    dart = bf.OpenDartReader(bf.DART_API_KEY)
    _log('재무 캐시 로드 중...')
    fin_cache = build_fin_cache(sb)
    requests.get = _counted_get
    _calls['n'], _calls['max'] = 0, a.max_calls

    ok = skip = fail = 0
    stop = None
    t0 = time.time()
    for i, (t, y, q) in enumerate(missing, 1):
        key = (t['stock_code'], y, q)
        try:
            if bf._collect_and_save(dart, sb, fin_cache, t, y, q):
                ok += 1
            else:
                skip += 1
                nodata.add(key)
        except BudgetExceeded as e:
            stop = str(e)
            break
        except Exception as e:
            fail += 1
            _log(f"  실패 {t['corp_name']} {y} {q}: {str(e)[:160]}")
        if i % 200 == 0:
            _log(f"  {i}/{len(missing)} · 성공 {ok} · 없음 {skip} · 실패 {fail} · DART {_calls['n']}건 · {time.time() - t0:.0f}초")
            state['nodata'] = sorted(nodata)
            json.dump(state, open(state_path, 'w'))
        time.sleep(0.12)

    requests.get = _orig_get
    state['nodata'] = sorted(nodata)
    state['runs'].append({'at': datetime.now().isoformat(timespec='seconds'), 'ok': ok, 'nodata': skip,
                          'fail': fail, 'calls': _calls['n'], 'stop': stop})
    json.dump(state, open(state_path, 'w'))
    left = len(missing) - (ok + skip + fail)
    _log(f"끝 — 성공 {ok} · DART 없음 {skip} · 실패 {fail} · DART {_calls['n']}건 · 남은 쌍 약 {left}"
         f"{' · 멈춘 이유: ' + stop if stop else ''} · {time.time() - t0:.0f}초")
    if stop and 'quota' in stop:
        _log('⚠ DART 사용한도(020) 도달 — 오늘 남은 공시 수집에 영향이 있을 수 있음')
    if not stop and left <= 0:
        _remove_cron()


if __name__ == '__main__':
    main()
