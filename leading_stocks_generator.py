"""
leading_stocks_generator.py
────────────────────────────
주도주 탐색기 — 매 영업일 장 마감 후 복합 스코어로
Top 50 주도주를 계산해 leading_stocks 테이블에 저장.

스코어 구성 (합계 max 100):
  price_momentum  (max 25) : 5일·20일·60일 수익률 백분위 + 추세 일관성 보너스
  volume_surge    (max 20) : 거래대금 배율 × 방향성 × 3일 지속성
  foreign_flow    (max 25) : 10일 누적 외국인 순매수 금액 백분위  (구 3일)
  sector_strength (max 15) : 업종 평균 대비 초과 수익률 백분위    (신규)
  hgpr_score      (max 15) : 52주 고가 근접도 연속 점수화

필터:
  시가총액 500억 이상
  당일 거래대금 100억 이상
  하락장 조정: 시장 전체 20일 평균 수익률이 -3% 미만이면 momentum 기준 강화

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
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
from db_utils import fetch_all_pages

load_dotenv()
from logger_config import get_logger
log = get_logger(__name__)

# ── Supabase 클라이언트 ──────────────────────────────────────────────────────
try:
    from supabase import create_client, Client
    SUPABASE_URL = os.environ.get('SB_URL', os.environ.get('SUPABASE_URL', ''))
    SUPABASE_KEY = os.environ.get('SB_SERVICE_KEY', os.environ.get('SUPABASE_KEY', ''))
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('SUPABASE_URL / SUPABASE_KEY 환경변수 미설정')
    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info('Supabase 연결 완료')
except Exception as e:
    log.error(f'Supabase 연결 실패: {e}')
    raise RuntimeError(f'leading_stocks_generator: Supabase 연결 실패 — {e}') from e

# ── 상수 ─────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS   = 70     # 최근 70일 소급 (60거래일 확보용)
TOP_N           = 50     # 저장할 상위 종목 수
MIN_MARKET_CAP  = 50_000_000_000    # 시가총액 500억 이상
MIN_TRADE_VALUE = 10_000_000_000    # 거래대금 100억 이상
HGPR_VALUES     = {'신고가', '52주 신고가', '1'}

# 하락장 판단 기준 (시장 평균 20일 수익률이 이 값 미만이면 하락장)
BEAR_MARKET_THRESHOLD = -3.0  # %


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
    """base_date 기준 최근 LOOKBACK_DAYS × 2 캘린더일 데이터 조회"""
    dt       = datetime.strptime(base_date, '%Y-%m-%d')
    from_dt  = dt - timedelta(days=LOOKBACK_DAYS * 2)
    from_str = from_dt.strftime('%Y-%m-%d')

    log.info(f'market_data 조회: {from_str} ~ {base_date}')
    rows = fetch_all_pages(
        sb.from_('market_data')
          .select('base_date,stock_code,corp_name,market,price,volume,'
                  'market_cap,foreign_net_buy,hgpr_cls_code,w52_high')
          .gte('base_date', from_str)
          .lte('base_date', base_date)
          .order('base_date', desc=False)
    )
    log.info(f'조회 완료: {len(rows):,}행')
    return rows


def fetch_industry_map() -> dict[str, str]:
    """companies 테이블에서 stock_code → industry 매핑"""
    rows = fetch_all_pages(
        sb.from_('companies')
          .select('code,industry')
          .not_.is_('industry', 'null')
    )
    ind_map = {
        (r.get('code') or '').replace('.KS', '').replace('.KQ', ''): r.get('industry')
        for r in rows
        if (r.get('code') or '').replace('.KS', '').replace('.KQ', '') and r.get('industry')
    }
    log.info(f'산업 매핑: {len(ind_map):,}개 종목')
    return ind_map


# ────────────────────────────────────────────────────────────────────────────
#  2. 헬퍼
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


def _is_bear_market(by_date: dict, trading_days: list, today_idx: int) -> bool:
    """
    시장 전체 평균 20일 수익률로 하락장 여부 판단.
    KOSPI 전 종목 평균을 시장 프록시로 사용.
    """
    if today_idx < 20:
        return False
    today_rows  = by_date[trading_days[today_idx]]
    prev20_rows = by_date[trading_days[today_idx - 20]]
    returns = []
    for code, today in today_rows.items():
        prev = prev20_rows.get(code)
        if prev and prev.get('price') and today.get('price'):
            r = (today['price'] / prev['price'] - 1) * 100
            returns.append(r)
    if not returns:
        return False
    avg_ret = sum(returns) / len(returns)
    log.info(f'시장 20일 평균 수익률: {avg_ret:.2f}% → {"하락장" if avg_ret < BEAR_MARKET_THRESHOLD else "보통장"}')
    return avg_ret < BEAR_MARKET_THRESHOLD


# ────────────────────────────────────────────────────────────────────────────
#  3. 스코어 계산
# ────────────────────────────────────────────────────────────────────────────
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

    # 하락장 여부 (전체 점수 보정용)
    bear_market = _is_bear_market(by_date, trading_days, today_idx)

    # ─ 종목별 지표 계산 ─
    records = []
    for stock_code, today in today_rows.items():
        price      = today.get('price') or 0
        volume     = today.get('volume') or 0
        market_cap = today.get('market_cap') or 0
        tv         = price * volume

        if market_cap < MIN_MARKET_CAP:
            continue
        if tv < MIN_TRADE_VALUE:
            continue

        corp_name = today.get('corp_name', '')
        market    = today.get('market', '')
        industry  = ind_map.get(stock_code, '')
        hgpr_cls  = str(today.get('hgpr_cls_code') or '')

        def price_n_ago(n: int):
            idx = today_idx - n
            if idx < 0:
                return None
            row = by_date[trading_days[idx]].get(stock_code)
            p   = row.get('price') if row else None
            return p if p and p > 0 else None

        # ── 수익률 ──
        p1  = price_n_ago(1)
        p5  = price_n_ago(5)
        p20 = price_n_ago(20)
        p60 = price_n_ago(min(60, today_idx))

        chg1  = round((price / p1  - 1) * 100, 2) if p1  else 0.0
        chg5  = round((price / p5  - 1) * 100, 2) if p5  else None
        chg20 = round((price / p20 - 1) * 100, 2) if p20 else None
        chg60 = round((price / p60 - 1) * 100, 2) if p60 else None

        # ── 추세 일관성: 5일·20일·60일 모두 양수인지 ──
        # 단기·중기·장기가 모두 우상향 = 진짜 주도주
        positive_count = sum(1 for c in [chg5, chg20, chg60] if c is not None and c > 0)
        # 3개 모두 양수: 1.25배, 2개: 1.0배, 1개 이하: 0.8배
        trend_consistency = 1.25 if positive_count == 3 else (1.0 if positive_count == 2 else 0.8)

        # ── 거래대금 배율 ──
        past_tvs = []
        for i in range(max(0, today_idx - 20), today_idx):
            r = by_date[trading_days[i]].get(stock_code)
            if r and r.get('volume') and r.get('price'):
                past_tvs.append(r['volume'] * r['price'])
        avg_tv    = sum(past_tvs) / len(past_tvs) if past_tvs else tv
        vol_ratio = round(tv / avg_tv, 2) if avg_tv else 1.0

        # 당일 방향성 페널티 (하락 + 거래대금 급증 = 패닉셀)
        dir_weight = 1.0 if chg1 >= 0 else 0.5

        # ── 거래대금 3일 연속 증가 여부 (지속성) ──
        # 주도주는 하루 이벤트가 아니라 꾸준히 거래가 늘어야 함
        tv_series = []
        for i in range(max(0, today_idx - 3), today_idx + 1):
            r = by_date[trading_days[i]].get(stock_code)
            if r and r.get('volume') and r.get('price'):
                tv_series.append(r['volume'] * r['price'])
        vol_continuous = (
            len(tv_series) >= 3 and
            all(tv_series[i] < tv_series[i+1] for i in range(len(tv_series)-1))
        )

        # ── 외국인 10일 누적 순매수 금액 (구 3일) ──
        # 3일은 단기 포지션 조정, 10일이 실제 추세 매수
        frgn_10d_amt = 0.0
        for i in range(max(0, today_idx - 9), today_idx + 1):
            r = by_date[trading_days[i]].get(stock_code)
            if r and r.get('foreign_net_buy') is not None:
                day_price = r.get('price') or price
                frgn_10d_amt += r['foreign_net_buy'] * day_price

        # ── 52주 고가 근접도 ──
        w52_high   = today.get('w52_high') or 0
        hgpr_ratio = (
            min(price / w52_high, 1.0) if w52_high and w52_high > 0
            else (1.0 if hgpr_cls in HGPR_VALUES else 0.7)
        )

        records.append({
            'stock_code':       stock_code,
            'corp_name':        corp_name,
            'market':           market,
            'industry':         industry,
            'price':            price,
            'market_cap':       market_cap,
            'tv':               tv,
            'chg1':             chg1,
            'chg5':             chg5,
            'chg20':            chg20,
            'chg60':            chg60,
            'trend_consistency':trend_consistency,
            'vol_ratio':        vol_ratio,
            'dir_weight':       dir_weight,
            'vol_continuous':   vol_continuous,
            'frgn_10d_amt':     round(frgn_10d_amt, 0),
            'hgpr_ratio':       round(hgpr_ratio, 4),
        })

    log.info(f'필터 통과: {len(records):,}개 (하락장={bear_market})')
    if not records:
        return []

    # ── 업종별 평균 5일 수익률 계산 (업종 상대 강도용) ──
    ind_chg5: dict[str, list] = defaultdict(list)
    for r in records:
        if r['industry'] and r['chg5'] is not None:
            ind_chg5[r['industry']].append(r['chg5'])
    ind_avg_chg5 = {
        ind: sum(vals) / len(vals)
        for ind, vals in ind_chg5.items() if vals
    }

    # ── 백분위 정규화용 전체 분포 ──
    chg5_vals   = [r['chg5']        for r in records]
    chg20_vals  = [r['chg20']       for r in records]
    chg60_vals  = [r['chg60']       for r in records]
    frgn_vals   = [r['frgn_10d_amt'] for r in records]

    # 업종 초과 수익률 분포 (sector_strength 백분위용)
    excess_vals = []
    for r in records:
        avg = ind_avg_chg5.get(r['industry'])
        excess = (r['chg5'] - avg) if (r['chg5'] is not None and avg is not None) else None
        excess_vals.append(excess)

    scored = []
    for i, r in enumerate(records):
        # ① 가격 모멘텀 (max 25)
        #    5일·20일·60일 백분위 복합 + 추세 일관성 보너스
        #    하락장에서는 momentum 기준 자동 강화 (상대 강도 더 중요)
        pct5  = _percentile_rank(chg5_vals,  r['chg5'])
        pct20 = _percentile_rank(chg20_vals, r['chg20'])
        pct60 = _percentile_rank(chg60_vals, r['chg60'])
        pct_mom = pct5 * 0.4 + pct20 * 0.4 + pct60 * 0.2
        raw_mom = pct_mom * r['trend_consistency']  # 추세 일관성 가중
        # 하락장: 상위 20% 이상 종목만 만점 가능하도록 기준 강화
        if bear_market:
            raw_mom = raw_mom * pct_mom  # 하락장 페널티 (상위권만 높은 점수)
        price_momentum = min(round(raw_mom * 25), 25)

        # ② 거래대금 (max 20)
        #    배율 × 방향성 × 지속성 보너스
        capped       = min(r['vol_ratio'], 5.0) / 5.0 * r['dir_weight']
        continuity   = 1.15 if r['vol_continuous'] else 1.0  # 3일 연속 증가 +15%
        volume_surge = min(round(capped * continuity * 20), 20)

        # ③ 외국인 수급 (max 25)
        #    10일 누적 금액 백분위
        pct_frgn     = _percentile_rank(frgn_vals, r['frgn_10d_amt'])
        foreign_flow = round(pct_frgn * 25)

        # ④ 업종 상대 강도 (max 15) — 신규
        #    동일 업종 평균 대비 초과 수익률 백분위
        #    업종 전체가 오를 때 업종 내 1등 = 진짜 주도주
        pct_excess      = _percentile_rank(excess_vals, excess_vals[i])
        sector_strength = round(pct_excess * 15)

        # ⑤ 52주 고가 근접도 (max 15)
        #    제곱으로 고근접 종목 차별화
        #    1.0(신고가) → 15점, 0.9 → 12점, 0.7 → 7점
        hgpr_score = min(round(r['hgpr_ratio'] ** 2 * 15), 15)

        total_score = price_momentum + volume_surge + foreign_flow + sector_strength + hgpr_score

        scored.append({
            **r,
            'total_score':    total_score,
            'price_momentum': price_momentum,
            'volume_surge':   volume_surge,
            'foreign_flow':   foreign_flow,
            'sector_strength':sector_strength,
            'hgpr_score':     hgpr_score,
            # DB 저장용 alias
            'frgn_3d':        round(r['frgn_10d_amt'] / max(r['price'], 1)),
        })

    # ─ 정렬 + 순위 ─
    scored.sort(key=lambda x: x['total_score'], reverse=True)
    for i, r in enumerate(scored[:TOP_N]):
        r['rank'] = i + 1

    top = scored[:TOP_N]
    log.info('Top 5 미리보기:')
    for r in top[:5]:
        log.info(
            f"  #{r['rank']:2d} {r['corp_name']:<12} 총{r['total_score']:3d}pt "
            f"(모멘텀{r['price_momentum']} 거래{r['volume_surge']} "
            f"외국인{r['foreign_flow']} 섹터{r['sector_strength']} 신고가{r['hgpr_score']}) "
            f"5d={r['chg5']}% 20d={r['chg20']}% consistency={r['trend_consistency']} "
            f"vol_cont={r['vol_continuous']} hgpr={r['hgpr_ratio']:.2f}"
        )
    return top


# ────────────────────────────────────────────────────────────────────────────
#  4. Supabase 저장
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
        'price_chg_60d':  r.get('chg60'),
        'volume_ratio':   r['vol_ratio'],
        'foreign_3d_sum': r.get('frgn_3d', 0),
        'market_cap':     r['market_cap'],
        'rank':           r.get('rank'),
    } for r in scored]

    log.info(f'Supabase upsert: {len(records)}개')
    sb.from_('leading_stocks') \
        .upsert(records, on_conflict='base_date,stock_code') \
        .execute()
    log.info('저장 완료')


# ────────────────────────────────────────────────────────────────────────────
#  5. 메인 실행
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
