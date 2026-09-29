"""태린이아빠 국내 전략 — 주도 업종 · RS · 후보 조건 (수급빈집 판정 직후, 18:50 잡)

원본: 2026-09-19 영상 「1년간 미장에서 52주 신고가 전략, 다음을 위해 전략 업그레이드」 1:30~7:00
  ① 주도 업종 = [6개월 수익률 ÷ 하방 표준편차 순위] ∩ [장 마감 후 기관·외국인 수급을 쪼개 보아 꾸준히 매수가
     들어오는 섹터] — '매수'는 2024-10 영상 방식("이때 매수만 본다": 사모·투신·연금·외국인 매수 순위)
     (11일선·20일선·10주선 위 여부도 같이 본다)
  ② 그 안에서 수급 오실레이터 빈집
  ③ 확률 높이기 — 거래대금 상위 · 컨센 상향 · 250일 신고가 중 하나
  일일 스크린 — RS 70 이상이면서 그날 거래대금(또는 기관·외국인 매수) 150위 안에서 수급이 빈 종목
  + 군집 현상(같은 색깔 종목들이 무리 지어 신고가)

구현
  - 업종 = WICS 중분류 28개. 지수는 FnGuide 공식 WICS 지수(wiseindex GetIndexPerformanceChart,
    term=3 → 6개월 일별 127점) — 원저자가 쓰는 퀀티와이즈 WICS/WI26 계열과 같은 원천.
  - 하방 표준편차 = sqrt(mean(min(일간수익률, 0)²)) (Sortino식 하방편차).
  - 매수 순위 = 최근 20거래일 사모·투신·연금 시총대비 + 사모·투신·연금·외국인 매수대금 7개 지표의 백분위
    평균(collect_flow_empty._rank_groups를 중분류 단위로). ⚠ 처음엔 기관+외국인 '순매수'가 플러스인 날 수로
    했는데 반도체(모멘텀 2위)가 20일 중 6일·누적 마이너스로 빠져, 원저자가 같은 시기 꼽은 반도체와 어긋났다.
    원저자는 '매수'를 본다 — 순매수 일수는 보드에 참고로만 남긴다.
  - RS = IBD 방식: 0.4×(3개월) + 0.2×(6개월) + 0.2×(9개월) + 0.2×(12개월) 가격비율 → 전 종목 백분위 1~99.
    KIS 주봉(수정주가) 1회 호출로 13·26·39·52주 전 종가를 얻는다.
  ⚠ 주도 업종 기준(모멘텀·매수 각 상위 10)과 RS 공식은 원본에 수치가 없어
    정한 값이다. 원저자의 RS 계산 코드는 멤버십 자료라 확인하지 못했다.

출력
  - leading_sectors: 판정일별 중분류 28개 보드
  - market_data.lead_flags(판정일 행): {lead, mid, rs, tv, tvu, nb, cons, nh, cl}
    tvu = 거래대금 상위150 ∧ 당일 상승, cl = 소분류 신고가 군집(09-19 회원 영상 — 후보 A의 OR 조건)
    후보 판정(빈집·후보A·후보B)은 화면이 수급 칸(flow_gauge)과 합쳐 계산한다(config.js flowIsEmpty·taerinEval).
"""

import logging
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = logging.getLogger(__name__)

WISE_URL = 'https://www.wiseindex.com/Index/GetIndexPerformanceChart'
WISE_HDR = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.wiseindex.com/Index/Index',
            'X-Requested-With': 'XMLHttpRequest'}

LEAD_MOM_TOP = 10    # 모멘텀 순위 상위 N개 중분류(28개 중 약 1/3)
LEAD_BUY_TOP = 10    # 매수 순위 상위 N개 중분류
FLOW_DAYS = 20       # 수급 꾸준함을 보는 거래일
FLOW_POS_MIN = 12    # 그중 기관+외국인 순매수가 플러스인 날이 이 이상(60%)이고 누적도 플러스
NH_DAYS = 5          # 신고가 군집을 세는 거래일
RANK_TOP = 150       # 거래대금·순매수 상위 기준(원본 "150등 이내")
CLUSTER_MIN = 3      # 신고가 군집 = 같은 WICS 소분류에서 최근 NH_DAYS일 52주 신고가 종목이 이 이상
                     # (원본에 수치 없음 — "같은 색깔 종목들이 무리 지어 신고가", 09-19 회원 영상)
CONS_DAYS = 30       # 컨센 상향으로 보는 최근 기간(달력일)
RS_WORKERS = 8


def _thread_client():
    from collect_flow_empty import _thread_client as tc
    return tc()


# ══════════════════════════════════════════════════════════════════════════════
#  ① 주도 업종
# ══════════════════════════════════════════════════════════════════════════════

def fetch_mid_series(dt: str, names: dict | None = None) -> dict:
    """WICS 중분류 28개 지수 6개월 일별 → {code: (name, [(date, close)…])}

    names = {중분류코드: companies.wics_mid} — 화면이 이 이름으로 거르므로 DB 표기를 우선한다
    (분류표는 '호텔,레스토랑,레저 등', DB는 '호텔,레스토랑,레저등'처럼 띄어쓰기가 다를 수 있다).
    """
    from collect_wics import WICS_MIDS
    names = names or {}

    def one(code):
        for attempt in range(3):
            try:
                r = requests.get(WISE_URL, params=dict(dt=dt, fromdt='', term='3', sec_cd=code, ceil_yn=0),
                                 headers=WISE_HDR, timeout=15)
                j = r.json()
                pts = [(datetime.utcfromtimestamp(ms / 1000 + 9 * 3600).strftime('%Y-%m-%d'), float(v))
                       for ms, v, *_ in j.get('data') or [] if v is not None]
                name = names.get(code) or WICS_MIDS[code]
                return code, (name, pts)
            except Exception as e:
                err = e
                time.sleep(1 + attempt)
        log.warning(f'[주도업종] {code} 지수 조회 실패: {err}')
        return code, (names.get(code) or WICS_MIDS[code], [])

    with ThreadPoolExecutor(4) as ex:
        return dict(ex.map(one, list(WICS_MIDS)))


def _ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def sector_momentum(series: dict) -> dict:
    """6개월 수익률 ÷ 하방 표준편차 + 이평선 위 여부 → {code: {...}}"""
    out = {}
    for code, (name, pts) in series.items():
        closes = [v for _, v in pts]
        if len(closes) < 60:
            continue
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        dd = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / len(rets))
        r6 = closes[-1] / closes[0] - 1
        last = closes[-1]
        ma = {n: _ma(closes, n) for n in (11, 20, 50)}
        out[code] = {
            'name': name, 'last_date': pts[-1][0], 'ret_6m': r6 * 100, 'down_dev': dd * 100,
            'score': (r6 / dd) if dd > 0 else None,
            'above_ma11': ma[11] is not None and last > ma[11],
            'above_ma20': ma[20] is not None and last > ma[20],
            'above_ma50': ma[50] is not None and last > ma[50],
            'ret_1d': (closes[-1] / closes[-2] - 1) * 100,
            # 시황 카드 6개월 추이 — 첫날=1 정규화
            'spark': [round(v / closes[0], 4) for v in closes],
        }
    ranked = sorted((c for c in out if out[c]['score'] is not None), key=lambda c: -out[c]['score'])
    for i, c in enumerate(ranked, 1):
        out[c]['mom_rank'] = i
    for c in out:
        out[c].setdefault('mom_rank', None)
        out[c]['n_sectors'] = len(ranked)
    return out


def _load_window(companies: list, since: str) -> list:
    """최근 구간 market_data — 업종 수급·신고가 군집·거래대금/순매수 순위용."""
    codes = [c['code'] for c in companies]

    def page(chunk):
        return fetch_all_pages(
            _thread_client().table('market_data')
              .select('stock_code,base_date,price,market_cap,trading_value,volume,'
                      'foreign_net_buy,institution_net_buy,hgpr_cls,price_change_rate,'
                      'pef_buy_amt,trust_buy_amt,pension_buy_amt')
              .in_('stock_code', chunk).gte('base_date', since)
              .order('base_date').order('stock_code'))
    rows = []
    with ThreadPoolExecutor(4) as ex:
        for part in ex.map(page, [codes[i:i + 200] for i in range(0, len(codes), 200)]):
            rows += part
    return rows


BUY3 = ('pef_buy_amt', 'trust_buy_amt', 'pension_buy_amt')   # 사모·투신·연기금 매수대금(백만원)


def sector_interest(rows: list, mid_of: dict, target: str) -> dict:
    """업종 관심도 — 원본 「외국인기관수급오실레이터 (업종)」 업종비중 시트.

    거래대금: 판정일 합, 최근 5·20거래일 평균(판정일 포함 — DataGuide 5일평균거래대금과 같게).
      원본은 '최근일 − 5일평균', '최근일 − 20일평균'(억원)을 본다.
    연기금·사모·투신 매수대금('1일 유동성 투여'): KIS가 전일까지만 줘 판정일보다 하루 늦은 날이
      최신이다(원본도 전일 열을 쓴다). 최근일 합과 최근 5거래일 평균.
      ⚠ 원본 '최근일 − 5일 평균'은 3투자자×5일=15칸 평균을 빼 평균이 1/3로 작아진다 — 일별 3투자자
      합의 5일 평균으로 고쳐 계산한다.
    """
    days = sorted({r['base_date'] for r in rows if r['base_date'] <= target})
    tv = defaultdict(lambda: defaultdict(float))
    buy = defaultdict(lambda: defaultdict(float))
    buy_rows = defaultdict(int)
    for r in rows:
        mid = mid_of.get(r['stock_code'])
        if not mid or r['base_date'] > target:
            continue
        if r['trading_value']:
            tv[mid][r['base_date']] += r['trading_value']
        vals = [r.get(k) for k in BUY3]
        if any(v is not None for v in vals):
            buy[mid][r['base_date']] += sum(v or 0 for v in vals) * 1e6
            buy_rows[r['base_date']] += 1
    # 매수대금이 '대부분 종목'에 있는 날만 — 일부만 채워진 날(수집 도중·당일)은 합이 작게 나온다
    top = max(buy_rows.values(), default=0)
    buy_days = [d for d in days if buy_rows.get(d, 0) >= top * 0.9] if top else []
    tv_days = days[-20:]
    out = {}
    for mid in set(tv) | set(buy):
        t = tv[mid]
        o = {'tv_now': t.get(target)}
        for n in (5, 20):
            w = [t[d] for d in tv_days[-n:] if d in t]
            o[f'tv_avg{n}'] = sum(w) / len(w) if len(w) == n else None
        if buy_days:
            b = buy[mid]
            bd = buy_days[-1]
            w = [b.get(d, 0.0) for d in buy_days[-5:]]
            o.update(buy_date=bd, buy3_now=b.get(bd, 0.0),
                     buy3_avg5=sum(w) / len(w) if len(w) == 5 else None)
        out[mid] = o
    return out


def nh_clusters(rows: list, ind_of: dict, target: str) -> dict:
    """WICS 소분류별 최근 NH_DAYS거래일 52주 신고가 종목 수 → {소분류: 수} (CLUSTER_MIN 이상만).

    원본 09-19 회원 영상: '어떤 섹터가 군집 현상이 있는데 그 군집 현상에 있는 것 중에서 빈집'이
    후보 A의 확률 높이기(OR) 조건 중 하나. 중분류(newhigh_5d)는 너무 넓어 소분류로 센다.
    """
    days = sorted({r['base_date'] for r in rows if r['base_date'] <= target})[-NH_DAYS:]
    hit = defaultdict(set)
    for r in rows:
        if r['base_date'] in days and r.get('hgpr_cls') == '신고가':
            ind = ind_of.get(r['stock_code'])
            if ind:
                hit[ind].add(r['stock_code'])
    return {ind: len(c) for ind, c in hit.items() if len(c) >= CLUSTER_MIN}


def sector_flows(rows: list, mid_of: dict, target: str):
    """업종별 최근 FLOW_DAYS일 기관+외국인 순매수 꾸준함 + 최근 NH_DAYS일 신고가 군집."""
    days = sorted({r['base_date'] for r in rows if r['base_date'] <= target})
    flow_days = [d for d in days
                 if sum(1 for r in rows if r['base_date'] == d and r['foreign_net_buy'] is not None) > 0][-FLOW_DAYS:]
    nh_days = set(days[-NH_DAYS:])
    fset = set(flow_days)

    daily = defaultdict(float)                 # (mid, date) → 순매수(원)
    cap = defaultdict(float)                   # mid → 판정일 시총
    nstk = defaultdict(set)
    nh = defaultdict(set)
    for r in rows:
        mid = mid_of.get(r['stock_code'])
        if not mid:
            continue
        d = r['base_date']
        if d in fset and r['price'] and (r['foreign_net_buy'] is not None or r['institution_net_buy'] is not None):
            daily[(mid, d)] += ((r['foreign_net_buy'] or 0) + (r['institution_net_buy'] or 0)) * r['price']
        if d == target:
            cap[mid] += r['market_cap'] or 0
            nstk[mid].add(r['stock_code'])
        if d in nh_days and r.get('hgpr_cls') == '신고가':
            nh[mid].add(r['stock_code'])

    out = {}
    for mid in set(m for m, _ in daily) | set(cap):
        vals = [daily.get((mid, d), 0.0) for d in flow_days]
        cum = sum(vals)
        out[mid] = {
            'flow_pos_days': sum(1 for v in vals if v > 0), 'flow_days': len(flow_days),
            'flow_cum': int(cum), 'flow_cum_ratio': (cum / cap[mid] * 100) if cap[mid] else None,
            'newhigh_5d': len(nh[mid]), 'n_stocks': len(nstk[mid]),
        }
    return out, flow_days


# ══════════════════════════════════════════════════════════════════════════════
#  RS — IBD 방식 (KIS 주봉, 수정주가)
# ══════════════════════════════════════════════════════════════════════════════

def _weekly_closes(code: str, start: str, end: str) -> list:
    """최근→과거 순 주봉 종가 목록(수정주가)."""
    from managers import kis_auth
    d = kis_auth.call_api(tr_id='FHKST03010100', path='quotations/inquire-daily-itemchartprice', code=code,
                          extra_params={'FID_INPUT_DATE_1': start, 'FID_INPUT_DATE_2': end,
                                        'FID_PERIOD_DIV_CODE': 'W', 'FID_ORG_ADJ_PRC': '0'})
    out = []
    for r in (d or {}).get('output2') or []:
        try:
            c = float(r.get('stck_clpr') or 0)
        except ValueError:
            c = 0
        if r.get('stck_bsop_date') and c > 0:
            out.append(c)
    return out   # [이번 주, 1주 전, …]


def rs_ratings(codes: list, price_now: dict) -> dict:
    """IBD RS 원점수 → 전 종목 백분위 1~99. 주봉 26주 미만(신규 상장)은 제외."""
    end = date.today().strftime('%Y%m%d')
    start = (date.today() - timedelta(days=400)).strftime('%Y%m%d')

    def one(code):
        try:
            w = _weekly_closes(code, start, end)
        except Exception:
            return code, None
        now = price_now.get(code) or (w[0] if w else None)
        if not now or len(w) < 27:
            return code, None
        anchors = [(13, 0.4), (26, 0.2), (39, 0.2), (52, 0.2)]
        num = den = 0.0
        for k, wt in anchors:
            if len(w) > k and w[k] > 0:
                num += wt * (now / w[k])
                den += wt
        return code, (num / den if den else None)

    raw = {}
    t0 = time.time()
    with ThreadPoolExecutor(RS_WORKERS) as ex:
        for code, v in ex.map(one, codes):
            if v is not None:
                raw[code] = v
    order = sorted(raw, key=lambda c: raw[c])
    n = len(order)
    rs = {c: max(1, min(99, int(round((i + 1) / n * 99)))) for i, c in enumerate(order)} if n else {}
    log.info(f'[RS] {len(codes)}종목 주봉 조회 {time.time() - t0:.0f}초 → RS {n}종목')
    return rs


def cons_up_codes(sb) -> set:
    """최근 CONS_DAYS일 안에 영업이익(없으면 매출) 추정치가 상향된 종목."""
    since = (date.today() - timedelta(days=CONS_DAYS)).isoformat()
    rows = fetch_all_pages(sb.table('estimate_revisions')
                           .select('stock_code,new_est_date,op_profit_change_pct,revenue_change_pct')
                           .gte('new_est_date', since).order('new_est_date').order('stock_code'))
    up = set()
    for r in rows:
        chg = r['op_profit_change_pct'] if r['op_profit_change_pct'] is not None else r['revenue_change_pct']
        if chg is not None and chg > 0:
            up.add(r['stock_code'])
    return up


# ══════════════════════════════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════════════════════════════

def _has_board_ext(sb) -> bool:
    """sql/sector_board.sql 실행 여부 — 없으면 오늘 등락·관심도 컬럼만 빼고 기록한다."""
    try:
        sb.table('leading_sectors').select('tv_now').limit(1).execute()
        return True
    except Exception:
        return False


def _has_schema(sb) -> bool:
    try:
        sb.table('market_data').select('lead_flags').limit(1).execute()
        sb.table('leading_sectors').select('mid_code').limit(1).execute()
        return True
    except Exception:
        return False


def buy_ranks(sb, mid_of: dict) -> dict:
    """중분류별 사모·투신·연금·외국인 매수 순위 → {mid: (rank, score)}"""
    from collect_flow_empty import _load_buy_rows, _rank_groups
    groups = {c: m for c, m in mid_of.items() if m}
    _per, rows, cinfo = _rank_groups(_load_buy_rows(sb, list(groups)), groups)
    if not rows:
        log.warning(f"[주도업종] 매수 순위 계산 불가: {cinfo.get('reason')}")
    return {r['grp']: (r['rank'], r['score']) for r in rows}


EMPTY_MAX_FILL = 2   # 빈집 = 수급 칸 2개 이하 (config.js FLOW_EMPTY_MAX_FILL과 같게)


def _is_empty(gauge: dict | None, pctl) -> bool:
    if gauge and gauge.get('lv') and gauge.get('cur') is not None:
        return sum(1 for v in gauge['lv'] if v is not None and gauge['cur'] >= v) <= EMPTY_MAX_FILL
    return pctl is not None and pctl < 50


def _count_stages(sb, board: dict, mid_of: dict, target: str):
    """중분류별 빈집·'이제 시작'(수급 단계) 종목 수 — 판정일 수급 칸을 읽는다.

    빈집 = 수급 칸 2개 이하(현재 오실레이터가 최근 63거래일 평균 아래) — 화면 config.js flowIsEmpty와
    같다(09-29 원본과 같게 칸으로 통일, 바꾸면 둘 다). 칸이 없는 행만 자기 이력 백분위 50 미만으로 대신한다.
    빈집 판정(collect_flow_empty)이 같은 잡에서 먼저 돈다.
    """
    from collect_flow_empty import _stage
    rows = fetch_all_pages(
        sb.table('market_data').select('stock_code,flow_pctl,flow_gauge')
          .eq('base_date', target).not_.is_('flow_quad', 'null').order('stock_code'))
    n_empty, n_start = defaultdict(int), defaultdict(int)
    for r in rows:
        mid = mid_of.get(r['stock_code'])
        if not mid:
            continue
        if _is_empty(r.get('flow_gauge'), r['flow_pctl']):
            n_empty[mid] += 1
        if _stage(r.get('flow_gauge')) == 'start':
            n_start[mid] += 1
    for c, b in board.items():
        b['n_empty'], b['n_start'] = n_empty[c], n_start[c]
    if not rows:
        log.warning(f'[주도업종] {target} 빈집 판정이 없어 업종별 빈집·이제 시작 수를 0으로 둡니다')


def run(dry: bool = False, rs: bool = True) -> dict:
    sb = get_supabase_client()
    log.info('=== [주도업종] 태린이아빠 전략 조건 계산 시작 ===')
    if not dry and not _has_schema(sb):
        log.warning('[주도업종] sql/leading_sectors.sql 미실행 — 건너뜀')
        return {}

    latest = sb.table('market_data').select('base_date').order('base_date', desc=True).limit(1).execute().data
    target = latest[0]['base_date']
    companies = fetch_all_pages(sb.table('companies').select('code,name,wics_code,wics_mid')
                                .eq('active', True).order('code'))
    mid_of = {c['code']: (c['wics_code'] or '')[:5] for c in companies if c.get('wics_code')}
    ind_of = {c['code']: c['wics_code'] for c in companies if c.get('wics_code')}   # 소분류 7자리
    mid_name = {}
    for c in companies:
        if c.get('wics_code') and c.get('wics_mid'):
            mid_name.setdefault(c['wics_code'][:5], c['wics_mid'])

    # ① 업종 모멘텀 (FnGuide WICS 지수)
    series = fetch_mid_series(target.replace('-', ''), mid_name)
    mom = sector_momentum(series)
    idx_last = max((m['last_date'] for m in mom.values()), default=None)
    if idx_last != target:
        log.warning(f'[주도업종] WICS 지수 최신일 {idx_last} ≠ 판정일 {target} — 지수 기준일로 계산')

    # ② 업종 수급 꾸준함 + 신고가 군집
    since = (date.fromisoformat(target) - timedelta(days=45)).isoformat()
    rows = _load_window(companies, since)
    flows, flow_days = sector_flows(rows, mid_of, target)
    interest = sector_interest(rows, mid_of, target)
    buys = buy_ranks(sb, mid_of)

    board = {}
    for code, m in mom.items():
        f = flows.get(code, {})
        br, bs = buys.get(code, (None, None))
        lead = bool(m['mom_rank'] and m['mom_rank'] <= LEAD_MOM_TOP and br and br <= LEAD_BUY_TOP)
        board[code] = dict(m, **f, **interest.get(code, {}), buy_rank=br, buy_score=bs, leading=lead)
    leads = [c for c in board if board[c]['leading']]
    _count_stages(sb, board, mid_of, target)
    log.info(f"[주도업종] 매수·순매수 {flow_days[0] if flow_days else '-'}~{flow_days[-1] if flow_days else '-'}"
             f"({len(flow_days)}일) · 주도 업종 {len(leads)}/{len(board)}: "
             + ' · '.join(f"{board[c]['name']}(모멘텀 {board[c]['mom_rank']}위·매수 {board[c]['buy_rank']}위)"
                          for c in sorted(leads, key=lambda c: board[c]['mom_rank'])))

    # ③ 종목 조건 — 판정일 행 기준
    today = [r for r in rows if r['base_date'] == target]
    tv_top = {r['stock_code'] for r in sorted((r for r in today if r['trading_value']),
                                              key=lambda r: -r['trading_value'])[:RANK_TOP]}
    nb_val = {r['stock_code']: ((r['foreign_net_buy'] or 0) + (r['institution_net_buy'] or 0)) * (r['price'] or 0)
              for r in today if r['foreign_net_buy'] is not None or r['institution_net_buy'] is not None}
    nb_top = {c for c, v in sorted(nb_val.items(), key=lambda kv: -kv[1])[:RANK_TOP] if v > 0}
    nh_today = {r['stock_code'] for r in today if r.get('hgpr_cls') == '신고가'}
    # 원본 09-19 회원 영상: "아침마다 거래대금이 상위이면서 주가가 플러스인 것들 리스트를 본다"
    tv_up = {r['stock_code'] for r in today
             if r['stock_code'] in tv_top and (r.get('price_change_rate') or 0) > 0}
    clusters = nh_clusters(rows, ind_of, target)
    cons = cons_up_codes(sb)
    price_now = {r['stock_code']: r['price'] for r in today if r['price']}
    rs_map = rs_ratings([c['code'] for c in companies], price_now) if rs else {}

    flags = {}
    for c in companies:
        code = c['code']
        if code not in price_now:
            continue
        mid = mid_of.get(code)
        flags[code] = {'lead': bool(mid and board.get(mid, {}).get('leading')), 'mid': mid,
                       'rs': rs_map.get(code), 'tv': code in tv_top, 'nb': code in nb_top,
                       'tvu': code in tv_up, 'cl': ind_of.get(code) in clusters,
                       'cons': code in cons, 'nh': code in nh_today}
    info = {'target': target, 'leads': [board[c]['name'] for c in leads], 'flags': len(flags),
            'lead_stocks': sum(1 for f in flags.values() if f['lead']),
            'rs70': sum(1 for f in flags.values() if (f['rs'] or 0) >= 70),
            'tv': len(tv_top), 'tvu': len(tv_up), 'nb': len(nb_top), 'cons': len(cons & set(flags)),
            'nh': len(nh_today), 'clusters': len(clusters),
            'cl': sum(1 for f in flags.values() if f['cl'])}
    log.info(f"[주도업종] 종목 조건 {info['flags']}종목 — 주도 업종 소속 {info['lead_stocks']} · RS70+ {info['rs70']}"
             f" · 거래대금150 {info['tv']}(상승 {info['tvu']}) · 순매수150 {info['nb']} · 컨센상향 {info['cons']}"
             f" · 신고가 {info['nh']} · 군집 소분류 {info['clusters']}개({info['cl']}종목)")
    if dry:
        return {'board': board, 'flags': flags, 'info': info}

    # 기록
    from collect_flow_empty import _patch_rows
    written = _patch_rows(sb, [{'stock_code': c, 'base_date': target, 'lead_flags': f} for c, f in flags.items()],
                          '주도업종')
    ext = _has_board_ext(sb)
    iv = lambda v: None if v is None else int(round(v))
    sb.table('leading_sectors').delete().eq('base_date', target).execute()
    sb.table('leading_sectors').insert([{
        'base_date': target, 'mid_code': c, 'name': b['name'],
        'ret_6m': round(b['ret_6m'], 2), 'down_dev': round(b['down_dev'], 3),
        'score': round(b['score'], 2) if b['score'] is not None else None,
        'mom_rank': b['mom_rank'], 'n_sectors': b['n_sectors'],
        'above_ma11': b['above_ma11'], 'above_ma20': b['above_ma20'], 'above_ma50': b['above_ma50'],
        'buy_rank': b.get('buy_rank'), 'buy_score': b.get('buy_score'),
        'flow_pos_days': b.get('flow_pos_days'), 'flow_days': b.get('flow_days'),
        'flow_cum': b.get('flow_cum'),
        'flow_cum_ratio': round(b['flow_cum_ratio'], 3) if b.get('flow_cum_ratio') is not None else None,
        'newhigh_5d': b.get('newhigh_5d'), 'n_stocks': b.get('n_stocks'), 'leading': b['leading'],
        'n_empty': b.get('n_empty'), 'n_start': b.get('n_start'), 'spark': b.get('spark'),
        **({'ret_1d': round(b['ret_1d'], 2) if b.get('ret_1d') is not None else None,
            'tv_now': iv(b.get('tv_now')), 'tv_avg5': iv(b.get('tv_avg5')), 'tv_avg20': iv(b.get('tv_avg20')),
            'buy3_now': iv(b.get('buy3_now')), 'buy3_avg5': iv(b.get('buy3_avg5')),
            'buy_date': b.get('buy_date')} if ext else {}),
    } for c, b in board.items()]).execute()
    log.info(f'[주도업종] {target} 종목 {written}행 · 보드 {len(board)}업종 기록')
    return {'info': info}


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    for _n in ('httpx', 'httpcore', 'hpack', 'urllib3'):
        logging.getLogger(_n).setLevel(logging.WARNING)
    run(dry='--dry' in sys.argv)
