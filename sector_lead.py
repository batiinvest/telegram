"""주도 업종 — 태린이아빠 2026-09 방식: 6개월 모멘텀 ∩ 기관·외국인 꾸준한 순매수

원본: 2026-09-25 영상 「미장 투자방식 일부 개선 신고가전략 + 빈집매수 보완」 1:00~1:35.
국내 주도 업종 = ① 6개월 수익률을 하방 표준편차로 나눈 값이 센 업종
              ∩ ② 장 마감 후 기관·외국인 수급으로 최근 꾸준히 매수가 발생한 업종.

① 업종 지수(sector_index)
   market_data는 120일만 보존해 6개월 가격 이력이 없다 → 업종별 시가총액 가중 일별 수익률을
   따로 쌓는다. 처음엔 KIS 일별시세(수정주가, 100거래일×2회)로 약 200거래일을 채우고
   (backfill_index), 이후엔 매일 market_data 등락률로 하루씩 이어 붙인다(update_index).
   가중치 = 전일 시가총액(백필은 전일 수정종가 × 현재 상장주식수). ±30%를 넘는 하루 수익률
   (신규상장 첫날 등)은 버린다.
   모멘텀 = 최근 126거래일 누적수익률 ÷ 하방 표준편차(√평균(min(r,0)²), 일간).

② 꾸준한 매수
   최근 20거래일 동안 업종 전체(외국인+기관 순매수 × 종가 합)가 순매수였던 날 수와,
   20일 순매수 합 ÷ 업종 시가총액(%). 두 값의 백분위 평균으로 순위.

주도 업종 = ① 상위 25% ∩ ② 상위 25%. 업종은 빈집 판정과 같은 비교 집단(WICS 업종,
10종목 미만은 중분류→섹터)이라 flow_concepts의 '사모·투신·연금·외국인 매수 순위'와 같은 행에
붙는다. 기간(20·126거래일)과 25%는 원본에 수치가 없어 우리가 정한 값이다.
"""

import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from db_utils import fetch_all_pages

log = logging.getLogger(__name__)

MOM_DAYS = 126    # 6개월(거래일)
FLOW_DAYS = 20    # 꾸준한 매수를 보는 최근 거래일
LEAD_TOP = 0.25   # 두 조건 각각 상위 25% (빈집 컨셉 필터와 같은 선)
RET_CLIP = 0.30   # 가격제한폭 밖 하루 수익률(신규상장 첫날 등)은 버린다
HIST_CALLS = 2    # 일별시세 100거래일 × 2회 ≈ 200거래일
WORKERS = 8


def has_schema(sb) -> bool:
    """sql/sector_lead.sql 실행 여부."""
    try:
        sb.table('sector_index').select('grp').limit(1).execute()
        sb.table('flow_concepts').select('lead').limit(1).execute()
        return True
    except Exception:
        return False


def _group_returns(items) -> dict:
    """[(집단, 수익률, 가중치)] → {집단: (가중 수익률, 종목 수)}"""
    acc = defaultdict(lambda: [0.0, 0.0, 0])
    for g, r, w in items:
        if not g or g == '전체' or r is None or not w or w <= 0 or abs(r) > RET_CLIP:
            continue
        a = acc[g]
        a[0] += r * w
        a[1] += w
        a[2] += 1
    return {g: (a[0] / a[1], a[2]) for g, a in acc.items() if a[1] > 0}


def _upsert_index(sb, rows: list) -> int:
    done = 0
    for i in range(0, len(rows), 1000):
        sb.table('sector_index').upsert(rows[i:i + 1000], on_conflict='grp,base_date').execute()
        done += len(rows[i:i + 1000])
    return done


def _fetch_closes(code: str, end_ymd: str) -> dict:
    """수정주가 일별 종가 {date: close} — 100거래일씩 HIST_CALLS번 거슬러 오른다."""
    from managers import kis_auth, safe_int
    out, end = {}, end_ymd
    for _ in range(HIST_CALLS):
        d = kis_auth.kis_get('FHKST03010100', 'quotations/inquire-daily-itemchartprice',
                             {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': code,
                              'FID_INPUT_DATE_1': '20000101', 'FID_INPUT_DATE_2': end,
                              'FID_PERIOD_DIV_CODE': 'D', 'FID_ORG_ADJ_PRC': '0'}, 'P', 10)
        rows = [r for r in ((d or {}).get('output2') or []) if r.get('stck_bsop_date')]
        if not rows:
            break
        for r in rows:
            c = safe_int(r.get('stck_clpr'), zero_as_none=True)
            b = r['stck_bsop_date']
            if c:
                out[f'{b[:4]}-{b[4:6]}-{b[6:]}'] = c
        if len(rows) < 100:
            break
        oldest = min(r['stck_bsop_date'] for r in rows)
        end = (datetime.strptime(oldest, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
    return out


def backfill_index(sb, groups: dict, write: bool = True) -> dict:
    """일회성 — 전종목 약 200거래일 수정주가로 업종 지수를 채운다. {집단: {날짜: (수익률, n)}}"""
    from collect_flow_empty import _thread_client
    codes = sorted(c for c, g in groups.items() if g and g != '전체')
    latest = sb.table('market_data').select('base_date').order('base_date', desc=True) \
               .limit(1).execute().data[0]['base_date']
    shares = {}
    for i in range(0, len(codes), 200):
        for r in fetch_all_pages(_thread_client().table('market_data')
                                 .select('stock_code,listing_shares').eq('base_date', latest)
                                 .in_('stock_code', codes[i:i + 200]).order('stock_code')):
            if r['listing_shares']:
                shares[r['stock_code']] = r['listing_shares']

    end = date.today().strftime('%Y%m%d')
    by_date = defaultdict(list)
    failed = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for n, (code, cl) in enumerate(zip(codes, ex.map(lambda c: _fetch_closes(c, end), codes)), 1):
            sh = shares.get(code)
            if not cl or not sh:
                failed += 1
                continue
            ds = sorted(cl)
            for a, b in zip(ds, ds[1:]):   # 종목의 연속 두 거래일(정지 구간은 한 번에)
                by_date[b].append((groups[code], cl[b] / cl[a] - 1, cl[a] * sh))
            if n % 500 == 0:
                log.info(f'[업종지수] 시세 {n}/{len(codes)}')

    series = defaultdict(dict)
    rows = []
    for d, items in by_date.items():
        for g, (r, k) in _group_returns(items).items():
            series[g][d] = (r, k)
            rows.append({'grp': g, 'base_date': d, 'ret': round(r, 6), 'n_stocks': k})
    log.info(f'[업종지수] 백필 {len(series)}업종 · {len(by_date)}거래일 · {len(rows)}행 · 시세 실패 {failed}종목')
    if write and rows:
        _upsert_index(sb, rows)
    return series


def update_index(sb, groups: dict, target: str) -> int:
    """매일 — target일 업종 수익률을 market_data 등락률·시가총액으로 계산해 이어 붙인다."""
    from collect_flow_empty import _thread_client
    codes = sorted(c for c, g in groups.items() if g and g != '전체')
    items = []
    for i in range(0, len(codes), 200):
        for r in fetch_all_pages(_thread_client().table('market_data')
                                 .select('stock_code,price_change_rate,market_cap').eq('base_date', target)
                                 .in_('stock_code', codes[i:i + 200]).order('stock_code')):
            rate, cap = r['price_change_rate'], r['market_cap']
            if rate is None or not cap:
                continue
            ret = rate / 100
            items.append((groups[r['stock_code']], ret, cap / (1 + ret)))   # 전일 시가총액 비중
    rows = [{'grp': g, 'base_date': target, 'ret': round(r, 6), 'n_stocks': k}
            for g, (r, k) in _group_returns(items).items()]
    return _upsert_index(sb, rows) if rows else 0


def _load_index(sb, target: str) -> dict:
    """{집단: [일별 수익률 …]} — target 이하, 날짜 오름차순."""
    since = (date.fromisoformat(target) - timedelta(days=MOM_DAYS * 2)).isoformat()
    rows = fetch_all_pages(sb.table('sector_index').select('grp,base_date,ret')
                           .gte('base_date', since).lte('base_date', target)
                           .order('grp').order('base_date'))
    series = defaultdict(list)
    for r in rows:
        series[r['grp']].append(float(r['ret']))
    return series


def momentum(series: dict) -> dict:
    """{집단: (6개월 수익률, 하방 표준편차, 수익률÷하방표준편차)} — 126거래일 미만은 뺀다."""
    out = {}
    for g, rets in series.items():
        if len(rets) < MOM_DAYS:
            continue
        w = rets[-MOM_DAYS:]
        level = 1.0
        for r in w:
            level *= 1 + r
        dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in w) / len(w))
        m6 = level - 1
        out[g] = (m6, dd, (m6 / dd) if dd > 0 else None)
    return out


def steady_flow(groups: dict, by_code: dict, flow_dates: list) -> dict:
    """{집단: (순매수 일수, 20일 순매수 ÷ 시가총액 %)} — 외국인+기관 순매수 × 종가."""
    from collect_flow_empty import _net
    fd = flow_dates[-FLOW_DAYS:]
    net = defaultdict(lambda: defaultdict(float))
    cap = defaultdict(float)
    for code, days in by_code.items():
        g = groups.get(code)
        if not g or g == '전체':
            continue
        caps = []
        for d in fd:
            x = days.get(d)
            n = _net(x)
            if n is None or x['p'] is None:
                continue
            net[g][d] += n * x['p']
            if x['cap']:
                caps.append(x['cap'])
        if caps:
            cap[g] += sum(caps) / len(caps)
    return {g: (sum(1 for d in fd if per.get(d, 0) > 0), sum(per.values()) / cap[g] * 100)
            for g, per in net.items() if cap[g] > 0}


def rank_sectors(mom: dict, flow: dict, universe: list) -> dict:
    """{집단: 주도 업종 판정 필드} — universe는 순위를 매길 집단(flow_concepts와 같은 목록)."""
    from collect_flow_empty import _pct_ranks
    ms = {g: mom[g][2] for g in universe if g in mom and mom[g][2] is not None}
    m_order = sorted(ms, key=lambda g: (-ms[g], g))
    m_rank = {g: i + 1 for i, g in enumerate(m_order)}

    fl = {g: flow[g] for g in universe if g in flow}
    if fl:
        p_pos = _pct_ranks({g: v[0] for g, v in fl.items()})
        p_net = _pct_ranks({g: v[1] for g, v in fl.items()})
        fs = {g: (p_pos[g] + p_net[g]) / 2 for g in fl}
    else:
        fs = {}
    f_order = sorted(fs, key=lambda g: (-fs[g], g))
    f_rank = {g: i + 1 for i, g in enumerate(f_order)}

    m_cut = max(1, round(len(m_order) * LEAD_TOP)) if m_order else 0
    f_cut = max(1, round(len(f_order) * LEAD_TOP)) if f_order else 0
    out = {}
    for g in universe:
        m = mom.get(g)
        f = flow.get(g)
        out[g] = {
            'mom_6m':    round(m[0] * 100, 2) if m else None,
            'down_dev':  round(m[1] * 100, 3) if m else None,
            'mom_score': round(m[2], 3) if m and m[2] is not None else None,
            'mom_rank':  m_rank.get(g),
            'pos_days':  f[0] if f else None,
            'net_ratio': round(f[1], 3) if f else None,
            'flow_rank': f_rank.get(g),
            'lead': bool(m_rank.get(g) and f_rank.get(g)
                         and m_rank[g] <= m_cut and f_rank[g] <= f_cut),
        }
    return out


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    for _n in ('httpx', 'httpcore', 'hpack', 'urllib3'):
        logging.getLogger(_n).setLevel(logging.WARNING)
    if '--backfill' in sys.argv:
        import collect_flow_empty as cfe
        sb = cfe.get_supabase_client()
        backfill_index(sb, cfe.static_groups(cfe._active_companies(sb)))
