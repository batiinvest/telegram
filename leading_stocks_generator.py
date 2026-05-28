"""
leading_stocks_generator.py
────────────────────────────
주도주 탐색기 — 매 영업일 장 마감 후 4개 지표 복합 스코어로
Top 50 주도주를 계산해 leading_stocks 테이블에 저장.

스코어 구성 (합계 max 100):
  price_momentum (max 30) : 5일/20일 수익률 복합 백분위 (5일 60%, 20일 40%)
  volume_surge   (max 25) : 당일 거래대금 / 20일 평균 배율 (최대 5배 캡)
  foreign_flow   (max 25) : 3일 누적 외국인 순매수 백분위
  hgpr_score     (max 20) : 52주 신고가 종목 일괄 +20

필터:
  시가총액 500억 이상 (5e10 원)
  당일 거래대금 100억 이상 (1e10 원 = volume × price)

Supabase 테이블: leading_stocks
  base_date, stock_code UNIQUE

사용법:
    python leading_stocks_generator.py              # 최신 날짜
    python leading_stocks_generator.py 2026-05-28   # 특정 날짜
    python leading_stocks_generator.py --watch       # app_config 플래그 감지 루프

의존:
    pip install supabase python-dotenv
    환경변수: SUPABASE_URL, SUPABASE_KEY (service role key)
"""

import os
import sys
import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

# ── Supabase 클라이언트 ──────────────────────────────────────────────────────
try:
    from supabase import create_client, Client
    SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
    SUPABASE_KEY = os.environ.get('SUPABASE_KEY', os.environ.get('SUPABASE_SERVICE_ROLE_KEY', ''))
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('SUPABASE_URL / SUPABASE_KEY 환경변수 미설정')
    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info('Supabase 연결 완료')
except Exception as e:
    log.error(f'Supabase 연결 실패: {e}')
    sys.exit(1)

# ── 상수 ─────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS   = 25     # 최근 25 거래일 조회
TOP_N           = 50     # 저장할 상위 종목 수
MIN_MARKET_CAP  = 50_000_000_000    # 시가총액 500억 이상 (원)
MIN_TRADE_VALUE = 10_000_000_000    # 거래대금 100억 이상 (원, volume×price)
HGPR_VALUES     = {'신고가', '52주 신고가', '1'}  # 신고가 코드


# ────────────────────────────────────────────────────────────────────────────
#  1. 데이터 조회
# ────────────────────────────────────────────────────────────────────────────
def get_latest_date(target: str | None) -> str:
    if target:
        return target
    res = sb.from_('market_data') \
        .select('base_date') \
        .order('base_date', desc=True) \
        .limit(1) \
        .execute()
    if not res.data:
        raise ValueError('market_data 비어있음')
    return res.data[0]['base_date']


def fetch_history(base_date: str) -> list[dict]:
    """base_date 기준 최근 LOOKBACK_DAYS 거래일 데이터 페이지네이션 조회"""
    dt       = datetime.strptime(base_date, '%Y-%m-%d')
    from_dt  = dt - timedelta(days=LOOKBACK_DAYS * 2)  # 여유 소급
    from_str = from_dt.strftime('%Y-%m-%d')

    log.info(f'market_data 조회: {from_str} ~ {base_date}')
    rows, offset = [], 0
    while True:
        res = sb.from_('market_data') \
            .select('base_date,stock_code,corp_name,market,price,volume,market_cap,foreign_net_buy,hgpr_cls_code') \
            .gte('base_date', from_str) \
            .lte('base_date', base_date) \
            .order('base_date', desc=False) \
            .range(offset, offset + 999) \
            .execute()
        chunk = res.data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    log.info(f'조회 완료: {len(rows):,}행')
    return rows


def fetch_industry_map() -> dict[str, str]:
    """companies 테이블에서 전체 stock_code → industry 매핑 (is_monitored 무관)"""
    ind_map, offset = {}, 0
    while True:
        res = sb.from_('companies') \
            .select('code,industry') \
            .not_.is_('industry', 'null') \
            .range(offset, offset + 999) \
            .execute()
        chunk = res.data or []
        for r in chunk:
            code = (r.get('code') or '').replace('.KS', '').replace('.KQ', '')
            ind  = r.get('industry')
            if code and ind:
                ind_map[code] = ind
        if len(chunk) < 1000:
            break
        offset += 1000
    log.info(f'산업 매핑: {len(ind_map):,}개 종목')
    return ind_map


# ────────────────────────────────────────────────────────────────────────────
#  2. 스코어 계산
# ────────────────────────────────────────────────────────────────────────────
def _percentile_rank(values: list, v) -> float:
    """v가 values 중 몇 번째 백분위인가 (0~1). None → 중립 0.5"""
    if v is None:
        return 0.5
    non_null = [x for x in values if x is not None]
    if not non_null:
        return 0.5
    below = sum(1 for x in non_null if x < v)
    return below / len(non_null)


def calc_scores(history: list[dict], target_date: str, ind_map: dict) -> list[dict]:
    # ─ 날짜별 그룹화 ─
    by_date: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in history:
        by_date[row['base_date']][row['stock_code']] = row

    trading_days = sorted(by_date.keys())
    if not trading_days or trading_days[-1] != target_date:
        log.warning(f'target_date({target_date})가 최신 거래일이 아님')
        return []

    today_rows = by_date[target_date]
    today_idx  = len(trading_days) - 1
    log.info(f'오늘({target_date}) 종목 수: {len(today_rows):,}')

    # ─ 종목별 지표 계산 ─
    records = []
    for stock_code, today in today_rows.items():
        price      = today.get('price') or 0
        volume     = today.get('volume') or 0
        market_cap = today.get('market_cap') or 0
        tv         = price * volume

        # 필터 적용
        if market_cap < MIN_MARKET_CAP:
            continue
        if tv < MIN_TRADE_VALUE:
            continue

        corp_name = today.get('corp_name', '')
        market    = today.get('market', '')
        industry  = ind_map.get(stock_code, '')
        hgpr_cls  = str(today.get('hgpr_cls_code') or '')

        # ① 가격 모멘텀: 5일/20일 수익률
        def price_n_ago(n: int):
            idx = today_idx - n
            if idx < 0:
                return None
            row = by_date[trading_days[idx]].get(stock_code)
            p   = row.get('price') if row else None
            return p if p and p > 0 else None

        p5  = price_n_ago(5)
        p20 = price_n_ago(20)
        chg5  = round((price / p5  - 1) * 100, 2) if p5  else None
        chg20 = round((price / p20 - 1) * 100, 2) if p20 else None

        # ② 거래대금 급증: today_tv / 20일 평균
        past_tvs = []
        for i in range(max(0, today_idx - 20), today_idx):
            r = by_date[trading_days[i]].get(stock_code)
            if r and r.get('volume') and r.get('price'):
                past_tvs.append(r['volume'] * r['price'])
        vol_ratio = round(tv / (sum(past_tvs) / len(past_tvs)), 2) if past_tvs else 1.0

        # ③ 외국인 3일 누적 순매수
        frgn_3d = 0.0
        for i in range(max(0, today_idx - 2), today_idx + 1):
            r = by_date[trading_days[i]].get(stock_code)
            if r and r.get('foreign_net_buy') is not None:
                frgn_3d += r['foreign_net_buy']

        # ④ 52주 신고가 여부
        is_hgpr = hgpr_cls in HGPR_VALUES

        records.append({
            'stock_code': stock_code,
            'corp_name':  corp_name,
            'market':     market,
            'industry':   industry,
            'price':      price,
            'market_cap': market_cap,
            'tv':         tv,
            'chg5':       chg5,
            'chg20':      chg20,
            'vol_ratio':  vol_ratio,
            'frgn_3d':    round(frgn_3d, 0),
            'is_hgpr':    is_hgpr,
        })

    log.info(f'필터 통과: {len(records):,}개')
    if not records:
        return []

    # ─ 백분위 정규화 ─
    chg5_vals  = [r['chg5']   for r in records]
    chg20_vals = [r['chg20']  for r in records]
    frgn_vals  = [r['frgn_3d'] for r in records]

    scored = []
    for r in records:
        pct5   = _percentile_rank(chg5_vals,  r['chg5'])
        pct20  = _percentile_rank(chg20_vals, r['chg20'])
        pct_mom = pct5 * 0.6 + pct20 * 0.4
        price_momentum = round(pct_mom * 30)

        capped       = min(r['vol_ratio'], 5.0) / 5.0
        volume_surge = round(capped * 25)

        pct_frgn     = _percentile_rank(frgn_vals, r['frgn_3d'])
        foreign_flow = round(pct_frgn * 25)

        hgpr_score   = 20 if r['is_hgpr'] else 0
        total_score  = price_momentum + volume_surge + foreign_flow + hgpr_score

        scored.append({
            **r,
            'total_score':    total_score,
            'price_momentum': price_momentum,
            'volume_surge':   volume_surge,
            'foreign_flow':   foreign_flow,
            'hgpr_score':     hgpr_score,
        })

    # 정렬 + 순위
    scored.sort(key=lambda x: x['total_score'], reverse=True)
    for i, r in enumerate(scored[:TOP_N]):
        r['rank'] = i + 1

    top = scored[:TOP_N]
    log.info(f'Top 5 미리보기:')
    for r in top[:5]:
        log.info(
            f"  #{r['rank']:2d} {r['corp_name']:<12} "
            f"총{r['total_score']:3d}pt "
            f"(가격{r['price_momentum']}+거래{r['volume_surge']}+외국인{r['foreign_flow']}+신고가{r['hgpr_score']})"
        )
    return top


# ────────────────────────────────────────────────────────────────────────────
#  3. Supabase 저장
# ────────────────────────────────────────────────────────────────────────────
def save_results(base_date: str, scored: list[dict]):
    if not scored:
        log.info('저장할 데이터 없음')
        return

    records = [{
        'base_date':      base_date,
        'stock_code':     r['stock_code'],
        'corp_name':      r['corp_name'],
        'market':         r['market'],
        'industry':       r['industry'] or None,
        'total_score':    r['total_score'],
        'price_momentum': r['price_momentum'],
        'volume_surge':   r['volume_surge'],
        'foreign_flow':   r['foreign_flow'],
        'hgpr_score':     r['hgpr_score'],
        'price_chg_5d':   r['chg5'],
        'price_chg_20d':  r['chg20'],
        'volume_ratio':   r['vol_ratio'],
        'foreign_3d_sum': r['frgn_3d'],
        'market_cap':     r['market_cap'],
        'rank':           r.get('rank'),
    } for r in scored]

    log.info(f'Supabase upsert: {len(records)}개')
    sb.from_('leading_stocks') \
        .upsert(records, on_conflict='base_date,stock_code') \
        .execute()
    log.info('저장 완료')


# ────────────────────────────────────────────────────────────────────────────
#  4. 메인 실행
# ────────────────────────────────────────────────────────────────────────────
def run(target_date: str | None = None):
    base_date = get_latest_date(target_date)
    log.info(f'=== 주도주 탐색기 생성 시작: {base_date} ===')
    ind_map = fetch_industry_map()
    history = fetch_history(base_date)
    scored  = calc_scores(history, base_date, ind_map)
    save_results(base_date, scored)
    log.info(f'=== 완료: {len(scored)}개 저장 ===')


def watch_mode():
    """app_config.run_leading_stocks_flag 폴링 루프"""
    FLAG_KEY = 'run_leading_stocks_flag'
    last_val = None
    log.info(f'watch 모드 — {FLAG_KEY} 감지 대기 중...')
    while True:
        try:
            res = sb.from_('app_config') \
                .select('value') \
                .eq('key', FLAG_KEY) \
                .limit(1) \
                .execute()
            val = (res.data or [{}])[0].get('value')
            if val and val != last_val:
                last_val = val
                log.info(f'플래그 감지: {val}')
                try:
                    run()
                except Exception as e:
                    log.error(f'실행 오류: {e}', exc_info=True)
        except Exception as e:
            log.warning(f'폴링 오류: {e}')
        time.sleep(30)


if __name__ == '__main__':
    if '--watch' in sys.argv:
        watch_mode()
    else:
        target = next((a for a in sys.argv[1:] if not a.startswith('--')), None)
        run(target)
