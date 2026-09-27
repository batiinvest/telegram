"""수급 '빈집' 판정 — 전종목 수급 정산 + market_data.flow_quad / flow_pctl 계산

기업분석 표의 '수급빈집' 칩이 읽는 값을 만든다. 두 단계로 돈다.

1) 수급 정산 (sync_recent)
   전 상장사의 외국인·기관 순매수를 KIS inquire-investor(30거래일)로 받아
   DB와 **값이 다른 행만** 고쳐 쓴다. 두 가지 이유가 있다.
   - 기존 수급 수집(16:45·18:15)은 모니터링 종목 + 거래대금 상위만 받는다.
     빈집은 63거래일 연속 이력이 있어야 판정되므로 전종목을 매일 쌓아야 한다.
   - **18:15에 받은 값은 최종값이 아니다.** KRX가 며칠 안에 고친다(2026-09-27 실측:
     모니터링 25종목 최근 8거래일의 88%가 수집값과 달랐고, 지금의 KIS 두 API·네이버는
     서로 일치. 삼성전자 09-22 외국인 수집 +747,617 → 현재 +659,851). 30일치를 다시
     받아 비교하면 수정분이 자동으로 반영된다 — 수급 지도·산업 요약도 같이 좋아진다.

2) 빈집 판정 (classify)
   정의는 수급 지도(js/flow-map.js) 빈집 모드와 같다 — 5일 오실레이터, 비교이력 63거래일,
   외국인+기관, 가로=공급강도 순위·세로=자기 이력 백분위. 단 하나 다른 것은 **순위를
   매기는 집단**이다.
   - 수급 지도: 화면에서 고른 테마(모니터링 종목)
   - 여기: 전종목이라 테마가 없는 종목이 많아 **WICS 업종**. 업종 종목이 MIN_PEERS 미만이면
     중분류 → 섹터 → 전체로 한 단계씩 올린다(09-27 기준 업종 79개 중 24개·115종목 해당).
   따라서 모니터링 종목은 세로(백분위)는 수급 지도와 같고, 가로(순위)는 비교 집단이
   달라 사분면이 다를 수 있다. 상수(OSC_N·SPAN·EMPTY_TH·MIN_SER)를 바꿀 때는
   flow-map.js(_FM_OSC_N·_FM_WINS 3M·_FM_EMPTY_TH)도 같이 바꿀 것.

일회성 백필: backfill(start) — 날짜 지정 API(FHPTJ04160001)로 보존 기간 전체를 채운다.
"""

import logging
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = logging.getLogger(__name__)

# ── 수급 지도와 공유하는 상수 (flow-map.js) ────────────────────────────────────
OSC_N = 5        # _FM_OSC_N — 오실레이터 롤링 창(태린이아빠 원 정의 5거래일 고정)
SPAN = 63        # _FM_WINS의 3M med — 비교 이력 창
EMPTY_TH = 30    # _FM_EMPTY_TH — ★(뚜렷한 빈집) 백분위 기준
MIN_SER = 8      # 백분위를 말할 최소 표본 (_fmRenderEmpty)

# ── 이 모듈 고유 ─────────────────────────────────────────────────────────────
MIN_PEERS = 10        # 순위를 매길 최소 집단 크기 — 이보다 작으면 한 단계 위 분류로
LOOKBACK_DAYS = 200   # 63거래일을 담기 위한 달력일 여유
SYNC_DAYS = 45        # 매일 정산 범위(달력일) — inquire-investor 30거래일을 덮는다
WORKERS = 8           # KIS 조회 동시성 (호출 한도는 managers.kis_rate_limiter가 지킨다)

QUADS = ('fill', 'full', 'bnce', 'cold')

_tl = threading.local()


def _thread_client():
    """스레드 전용 Supabase 클라이언트.

    get_supabase_client()는 싱글톤이라 HTTP/2 연결 하나를 공유한다. 여러 스레드가 동시에
    쓰면 연결 상태(hpack)가 깨져 ConnectionTerminated(PROTOCOL/COMPRESSION_ERROR)로
    조회·쓰기가 끊긴다(09-27 백필에서 실측). 병렬 구간은 스레드마다 자기 클라이언트를 쓴다.
    """
    c = getattr(_tl, 'sb', None)
    if c is None:
        import db_client
        from supabase import create_client
        c = _tl.sb = create_client(db_client._resolve(db_client._URL_KEYS),
                                   db_client._resolve(db_client._KEY_KEYS))
    return c
PEER_LEVELS = (('업종', 'wics_industry'), ('중분류', 'wics_mid'), ('섹터', 'wics_sector'))


def _quad(x: float, y: float) -> str:
    """_fmQuadE와 동일 — 가로=공급강도 순위, 세로=자기 이력 대비 현재 위치."""
    if x >= 0:
        return 'fill' if y < 0 else 'full'
    return 'cold' if y < 0 else 'bnce'


def _net(day: dict | None) -> float | None:
    """외국인+기관 순매수 주식수 (FM.inv='both' 기본값과 동일)."""
    if not day:
        return None
    f, i = day['f'], day['i']
    if f is None and i is None:
        return None
    return (f or 0) + (i or 0)


def _osc_series(days: dict, dates: list) -> list:
    """osc(d) = Σ(d-4..d) 순매수대금 ÷ 당일 시가총액 × 100(%)  — _fmOscSeries와 동일.

    분모를 '당일' 시총으로 잡아 기간 중 주가가 크게 변한 종목의 왜곡을 없앤다.
    창이 덜 찬 날(결측 포함)은 버려 5일 합산의 의미를 지킨다.
    """
    out = []
    for i in range(OSC_N - 1, len(dates)):
        di = days.get(dates[i])
        cap = di['cap'] if di else None
        if not cap or cap <= 0:
            continue
        total, hit = 0.0, 0
        for k in range(i - OSC_N + 1, i + 1):
            d = days.get(dates[k])
            n = _net(d)
            if n is None or d['p'] is None:
                continue
            total += n * d['p']
            hit += 1
        if hit < OSC_N:
            continue
        out.append((dates[i], total / cap * 100))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  1) 수급 정산
# ══════════════════════════════════════════════════════════════════════════════

def _active_companies(sb) -> list:
    return fetch_all_pages(
        sb.table('companies').select('code,wics_industry,wics_mid,wics_sector')
          .eq('active', True).order('code'))   # 고유 정렬 — 페이지 경계에서 행이 안 빠진다


def _db_flow(sb, codes: list, since: str) -> dict:
    """{(code, date): (foreign, institution)} — 수급이 NULL인 행도 포함(존재 확인용)."""
    def page(chunk):
        return fetch_all_pages(
            _thread_client().table('market_data')
              .select('stock_code,base_date,foreign_net_buy,institution_net_buy')
              .in_('stock_code', chunk).gte('base_date', since)
              .order('base_date').order('stock_code'))
    chunks = [codes[i:i + 200] for i in range(0, len(codes), 200)]
    out = {}
    with ThreadPoolExecutor(4) as ex:
        for rows in ex.map(page, chunks):
            for r in rows:
                out[(r['stock_code'], r['base_date'])] = (r['foreign_net_buy'], r['institution_net_buy'])
    return out


def _write_flow(sb, rows: list) -> int:
    """수급 행 쓰기 — RPC 일괄 UPDATE 우선, 없으면 행 단위 UPDATE를 병렬로.

    부분 컬럼 upsert는 못 쓴다 — PostgREST가 INSERT 후보의 corp_name NOT NULL을
    충돌 판정보다 먼저 검사해 23502로 실패한다. sql/bulk_update_flow.sql의 함수가 있으면
    1,000행을 한 번에 쓰고, 없으면 행마다 UPDATE한다(존재하는 행만 — 스켈레톤 행 없음).
    """
    done = 0
    for i in range(0, len(rows), 1000):
        chunk = rows[i:i + 1000]
        try:
            n = sb.rpc('bulk_update_market_flow', {'payload': chunk}).execute().data
            if isinstance(n, int):
                done += n
                continue
        except Exception:
            pass   # 함수 미설치(PGRST202) 등 — 행 단위로

        def one(r):
            # 공유 클라이언트를 여러 스레드가 쓰면 가끔 응답 단계에서 끊긴다(HTTP/2 스트림).
            # 같은 값을 다시 쓰는 UPDATE라 재시도해도 안전하다
            for attempt in range(3):
                try:
                    _thread_client().table('market_data').update({
                        'foreign_net_buy': r['foreign_net_buy'],
                        'institution_net_buy': r['institution_net_buy'],
                    }).eq('stock_code', r['stock_code']).eq('base_date', r['base_date']).execute()
                    return 1
                except Exception as e:
                    err = e
                    time.sleep(0.3 * (attempt + 1))
            log.warning(f"[수급정산] {r['stock_code']} {r['base_date']} 쓰기 실패: {str(err)[:120]}")
            return 0
        with ThreadPoolExecutor(WORKERS) as ex:
            done += sum(ex.map(one, chunk))
    return done


def _diff(fetched: dict, db: dict) -> list:
    """KIS 값과 DB 값이 다른 '존재하는' 행만 골라 쓰기 목록으로."""
    rows = []
    for (code, d), (f, i) in fetched.items():
        cur = db.get((code, d))
        if cur is None or cur == (f, i):   # 행 없음(휴장·미상장) 또는 이미 같음
            continue
        rows.append({'stock_code': code, 'base_date': d,
                     'foreign_net_buy': f, 'institution_net_buy': i})
    return rows


def sync_recent(sb, codes: list) -> dict:
    """매일 — 전종목 최근 30거래일 수급을 KIS와 맞춘다. 새 날짜 채움 + 사후 수정 반영."""
    from collect_market import fetch_investor_history   # {date: (foreign, institution)}
    t0 = time.time()
    since = (date.today() - timedelta(days=SYNC_DAYS)).isoformat()

    fetched, failed = {}, 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for code, hist in zip(codes, ex.map(fetch_investor_history, codes)):
            if not hist:
                failed += 1
            for d, fi in hist.items():
                if d >= since:
                    fetched[(code, d)] = fi
    t1 = time.time()

    db = _db_flow(sb, codes, since)
    rows = _diff(fetched, db)
    filled = sum(1 for r in rows if db[(r['stock_code'], r['base_date'])] == (None, None))
    written = _write_flow(sb, rows)
    log.info(f"[수급정산] {len(codes)}종목 조회 {t1 - t0:.0f}초(실패 {failed}) · "
             f"변경 {len(rows)}행(신규 {filled} · 수정 {len(rows) - filled}) · "
             f"기록 {written}행 · 총 {time.time() - t0:.0f}초")
    return {'codes': len(codes), 'failed': failed, 'changed': len(rows),
            'filled': filled, 'written': written}


def _fetch_daily(code: str, ymd: str) -> dict:
    """FHPTJ04160001 — ymd부터 과거로 30거래일. {date: (foreign, institution)}"""
    from managers import kis_auth, safe_int
    d = kis_auth.call_api(tr_id='FHPTJ04160001', path='quotations/investor-trade-by-stock-daily',
                          code=code, extra_params={'FID_INPUT_DATE_1': ymd, 'FID_ORG_ADJ_PRC': '',
                                                   'FID_ETC_CLS_CODE': ''})
    out = {}
    for r in (d or {}).get('output2') or []:
        b = (r.get('stck_bsop_date') or '').strip()
        f = str(r.get('frgn_ntby_qty') or '').strip()
        o = str(r.get('orgn_ntby_qty') or '').strip()
        if len(b) == 8 and (f or o):
            out[f'{b[:4]}-{b[4:6]}-{b[6:]}'] = (safe_int(f), safe_int(o))
    return out


def backfill(start: str, codes: list | None = None) -> dict:
    """일회성 — start(YYYY-MM-DD)부터 전종목 수급을 날짜 지정 API로 채우고 맞춘다.

    inquire-investor는 최근 30거래일만 주므로 보존 기간(63거래일+)을 못 채운다.
    FHPTJ04160001은 기준일을 줄 수 있어 30거래일씩 거슬러 올라간다(종목당 3회 안팎).
    두 API는 같은 날짜에 같은 값을 준다(09-27 실측, 25종목×59일 수정분까지 일치).
    """
    sb = get_supabase_client()
    codes = codes or [c['code'] for c in _active_companies(sb)]
    t0 = time.time()

    # 행이 없는 날은 받아도 쓸 곳이 없다 — 종목별로 행이 있는 가장 이른 날까지만 간다.
    # 비모니터링 종목은 보존이 28일이던 시절 탓에 08-24부터만 행이 있어 호출 1회로 끝난다.
    db = _db_flow(sb, codes, start)
    earliest = {}
    for (c, d) in db:
        if c not in earliest or d < earliest[c]:
            earliest[c] = d
    log.info(f"[수급백필] DB 행 {len(db)}개 확인 ({time.time() - t0:.0f}초)")

    def walk(code):
        stop = earliest.get(code)
        if not stop:
            return code, {}
        # 기준일이 오늘이면 15:40 이후 'TIME LIMIT 00:00 ~ 15:40'(rt_cd 2)으로 거부된다(09-27 실측).
        # 어제부터 거슬러 올라간다 — 휴장일을 줘도 직전 거래일부터 돌려준다. 당일분은 sync_recent 몫.
        got, anchor = {}, date.today() - timedelta(days=1)
        for _ in range(6):   # 안전장치 — 보존 120일이면 3~4회면 끝난다
            part = _fetch_daily(code, anchor.strftime('%Y%m%d'))
            if not part:
                break
            got.update(part)
            oldest = min(part)
            if oldest <= stop:
                break
            anchor = datetime.strptime(oldest, '%Y-%m-%d').date() - timedelta(days=1)
        return code, {d: v for d, v in got.items() if d >= stop}

    fetched, failed = {}, 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for n, (code, hist) in enumerate(ex.map(walk, codes), 1):
            if not hist and code in earliest:
                failed += 1
            for d, fi in hist.items():
                fetched[(code, d)] = fi
            if n % 250 == 0:
                log.info(f"[수급백필] 조회 {n}/{len(codes)} ({time.time() - t0:.0f}초)")
    t1 = time.time()

    rows = _diff(fetched, db)
    filled = sum(1 for r in rows if db[(r['stock_code'], r['base_date'])] == (None, None))
    log.info(f"[수급백필] 조회 {t1 - t0:.0f}초 · 쓰기 대상 {len(rows)}행 "
             f"(신규 {filled} · 수정 {len(rows) - filled}) — 기록 시작")
    written = _write_flow(sb, rows)
    log.info(f"[수급백필] 완료 — {written}행 기록 · 조회실패 {failed}종목 · 총 {time.time() - t0:.0f}초")
    return {'codes': len(codes), 'failed': failed, 'changed': len(rows),
            'filled': filled, 'written': written}


# ══════════════════════════════════════════════════════════════════════════════
#  2) 빈집 판정
# ══════════════════════════════════════════════════════════════════════════════

def _load_flow(sb, codes: list, cutoff: str):
    """수급이 있는 행 → ({code: {date: {f,i,p,cap}}}, 거래일 목록)."""
    def page(chunk):
        return fetch_all_pages(
            _thread_client().table('market_data')
              .select('stock_code,base_date,price,market_cap,'
                      'foreign_net_buy,institution_net_buy')
              .in_('stock_code', chunk).gte('base_date', cutoff)
              .not_.is_('foreign_net_buy', 'null')
              # (base_date, stock_code) 총순서 — PK 기준이라 페이지 경계에서 행이 안 섞인다
              .order('base_date').order('stock_code'))
    chunks = [codes[i:i + 200] for i in range(0, len(codes), 200)]
    by_code, dates = defaultdict(dict), set()
    with ThreadPoolExecutor(4) as ex:
        for rows in ex.map(page, chunks):
            for r in rows:
                dates.add(r['base_date'])
                by_code[r['stock_code']][r['base_date']] = {
                    'f': r['foreign_net_buy'], 'i': r['institution_net_buy'],
                    'p': r['price'], 'cap': r['market_cap'],
                }
    return by_code, sorted(dates)


def _common_start(by_code: dict, dates: list) -> str:
    """비교 창의 시작일 — 최근 SPAN 거래일과 '대부분 종목이 수급을 가진 첫날' 중 늦은 쪽.

    이력 길이가 다른 종목을 한 집단에서 순위 매기면 서로 다른 기간의 평균을 견주게 된다
    (모니터링 62거래일 vs 비모니터링 23거래일, 09-27 기준). 창을 공통 구간으로 맞춰
    가로(평균 순위)·세로(자기 백분위) 모두 같은 기간을 보게 한다. 보존 120일이라
    공통 구간은 매일 하루씩 늘어 SPAN(63)에 닿으면 수급 지도 3M과 같은 창이 된다.
    """
    per_day = defaultdict(int)
    for days in by_code.values():
        for d in days:
            per_day[d] += 1
    top = max(per_day.values())
    common = next(d for d in dates if per_day[d] >= top * 0.9)
    return max(common, dates[max(0, len(dates) - SPAN)])


def _stat(days: dict, dates: list, cut_date: str):
    """종목 하나의 (cur, avg, pct) — 수급 지도 _fmRenderEmpty 1차와 동일. 판정 불가면 None."""
    ser = [o for o in _osc_series(days, dates) if o[0] >= cut_date]
    if len(ser) < MIN_SER:
        return None, 'short'
    # 마지막 거래일 값이 없으면 어제 판정을 오늘 행에 적게 된다 — 적지 않는다
    if ser[-1][0] != dates[-1]:
        return None, 'stale'
    vals = [v for _, v in ser]
    cur = vals[-1]
    return {'cur': cur, 'avg': statistics.fmean(vals),
            'pct': sum(1 for v in vals if v < cur) / len(vals) * 100}, None


def _rank_within(stats: dict, groups: dict) -> dict:
    """집단별 평균 오실레이터 순위 → {code: (quad, pctl)}. groups = {code: 집단키}."""
    members = defaultdict(list)
    for code, g in groups.items():
        members[g].append(code)
    out = {}
    for g, codes in members.items():
        n = len(codes)
        avgs = [stats[c]['avg'] for c in codes]
        for c in codes:
            p = stats[c]
            # 가로축 = 집단 내 공급강도 순위. 평균의 절대 부호로 가르면 0 근처에 몰려
            # 좌/우가 잡음으로 갈리므로 순위로 환산한다(수급 지도와 같은 이유).
            sup = (sum(1 for a in avgs if a < p['avg']) / n * 100) if n > 1 else 50
            out[c] = (_quad(sup - 50, p['pct'] - 50), round(p['pct']))
    return out


def _peer_groups(stats: dict, meta: dict) -> tuple:
    """종목마다 비교 집단 결정 — 업종이 MIN_PEERS 미만이면 중분류 → 섹터 → 전체."""
    groups, level_n = {}, defaultdict(int)
    pending = set(stats)
    for label, col in PEER_LEVELS:
        size = defaultdict(int)
        for c in pending:
            v = (meta.get(c) or {}).get(col)
            if v:
                size[v] += 1
        for c in list(pending):
            v = (meta.get(c) or {}).get(col)
            if v and size[v] >= MIN_PEERS:
                groups[c] = f'{label}:{v}'
                level_n[label] += 1
                pending.discard(c)
    for c in pending:
        groups[c] = '전체'
        level_n['전체'] += 1
    return groups, dict(level_n)


def classify(sb, companies: list | None = None):
    """전종목 빈집 판정 → ({code: (quad, pctl)}, 수급 최신일, 통계)."""
    companies = companies or _active_companies(sb)
    meta = {c['code']: c for c in companies}
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    by_code, dates = _load_flow(sb, list(meta), cutoff)
    if len(dates) < OSC_N + 3:
        log.warning(f'[빈집] 수급 이력 {len(dates)}거래일 — 판정 불가')
        return {}, (dates[-1] if dates else None), {}

    cut_date = _common_start(by_code, dates)
    stats, skipped = {}, defaultdict(int)
    for code, days in by_code.items():
        s, why = _stat(days, dates, cut_date)
        if s:
            stats[code] = s
        else:
            skipped[why] += 1
    groups, level_n = _peer_groups(stats, meta)
    verdicts = _rank_within(stats, groups)

    n_win = sum(1 for d in dates if d >= cut_date)
    info = {'dates': len(dates), 'window': f'{cut_date}~{dates[-1]} ({n_win}거래일)',
            'with_flow': len(by_code), 'judged': len(verdicts),
            'skipped': dict(skipped), 'levels': level_n}
    log.info(f"[빈집] 수급 보유 {len(by_code)}종목 → 판정 {len(verdicts)} "
             f"(제외: 표본부족 {skipped['short']} · 최신일 수급없음 {skipped['stale']}) · "
             f"창 {info['window']} · 비교집단 " + ' · '.join(f'{k} {v}' for k, v in level_n.items()))
    return verdicts, dates[-1], info


def _write_verdicts(sb, verdicts: dict, target_date: str) -> int:
    """(quad, pctl) 같은 종목끼리 묶어 일괄 PATCH — 부분 upsert는 23502라 못 쓴다."""
    groups = defaultdict(list)
    for code, (quad, pctl) in verdicts.items():
        groups[(quad, pctl)].append(code)

    def put(item):
        (quad, pctl), codes = item
        n = 0
        for i in range(0, len(codes), 200):
            d = _thread_client().table('market_data') \
                  .update({'flow_quad': quad, 'flow_pctl': pctl}) \
                  .eq('base_date', target_date) \
                  .in_('stock_code', codes[i:i + 200]).execute().data
            n += len(d or [])
        return n
    with ThreadPoolExecutor(4) as ex:
        done = sum(ex.map(put, groups.items()))
    log.info(f"[빈집] {target_date} {done}행 기록 ({len(groups)}회 UPDATE)")
    return done


def run(dry: bool = False, sync: bool = True) -> int:
    """매일 18:50 — 전종목 수급 정산 후 당일 market_data 행에 빈집 판정을 적는다."""
    sb = get_supabase_client()
    log.info('=== [빈집] 수급 정산 + 빈집 판정 시작 ===')
    if not dry:
        # 컬럼이 없으면 계산을 다 해놓고 쓰기에서 PGRST204로 끊긴다 — 먼저 확인해 원인을 바로 알린다
        try:
            sb.table('market_data').select('flow_quad,flow_pctl').limit(1).execute()
        except Exception as e:
            raise RuntimeError('market_data.flow_quad/flow_pctl 컬럼 없음 — '
                               'sql/flow_empty.sql을 먼저 실행하세요') from e

    companies = _active_companies(sb)
    if sync and not dry:
        sync_recent(sb, [c['code'] for c in companies])

    verdicts, last_flow, _ = classify(sb, companies)
    if not verdicts:
        log.warning('[빈집] 판정 결과 없음 — 수급 이력을 확인하세요')
        return 0

    cnt = defaultdict(int)
    for q, _p in verdicts.values():
        cnt[q] += 1
    deep = sum(1 for q, p in verdicts.values() if q == 'fill' and p <= EMPTY_TH)
    log.info(f"[빈집] 판정 {len(verdicts)}종목 — "
             + ' · '.join(f'{q} {cnt[q]}' for q in QUADS)
             + f" (★뚜렷한 빈집 {deep})")

    # 표가 읽는 날짜에 적는다 — 표는 market_data 최신일 행만 조회한다.
    latest = sb.table('market_data').select('base_date') \
               .order('base_date', desc=True).limit(1).execute().data
    target = latest[0]['base_date'] if latest else None
    if not target:
        log.error('[빈집] market_data 최신일을 찾을 수 없음')
        return 0
    if target != last_flow:
        # 수급이 아직 안 들어온 날 — 판정은 last_flow 기준이라 하루 묵는다.
        log.warning(f"[빈집] 수급 최신일 {last_flow} ≠ 표 기준일 {target} "
                    f"— {last_flow} 기준 판정을 {target} 행에 적습니다")

    if dry:
        log.info(f"[빈집] dry-run — {target} 행에 {len(verdicts)}건 기록 예정 (쓰기 생략)")
        return len(verdicts)
    return _write_verdicts(sb, verdicts, target)


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    for _n in ('httpx', 'httpcore', 'hpack'):
        logging.getLogger(_n).setLevel(logging.WARNING)
    if '--backfill' in sys.argv:
        # 예: python3 collect_flow_empty.py --backfill 2026-06-25
        backfill(sys.argv[sys.argv.index('--backfill') + 1])
    else:
        run(dry='--dry' in sys.argv, sync='--no-sync' not in sys.argv)
