"""한국 업종 ETF 소르티노 · 상대강도 · 52주 신고가 전략 → market_snapshot(kind='kr_etf')

[원본] 태린이아빠 Market Dashboard '한국 ETF 소라티노 및 상대강도'(공개 소스 app.py KR_SECTOR_SRC).
  원본은 관리자가 엑셀('데이터' 시트: DATE · 코스피 · 종목 42열)을 올려 계산한다. 여기서는 같은 42종목(업종 ETF 38 +
  삼성전자 · SK하이닉스 · 파마리서치 · 에이피알)과 코스피 지수의 일별 종가를 야후(코드.KS · .KQ, 수정 전 종가)로 받는다.
  야후 종가는 KRX 공식 종가와 같음을 확인했다(2026-11-17 KODEX 반도체 87,600 · 삼성전자 190,300 · 코스피 4,968.48).

  ① Mansfield RS(126일) = (종목 ÷ 코스피) ÷ 그 126일 평균 − 1, ×100.  정규화 = 100 ÷ (1 + e^(−RS ÷ 12)), 70 이상이 강함.
     6개월 수익률(126거래일)과 코스피 대비 차이.
  ② 최근 10거래일 순위(각 상위 20)
     변동성 조정 모멘텀 = 126일 창의 수익률(마지막 ÷ 첫 값 − 1) ÷ 일간 수익률 표준편차
     소르티노(126일, 연율) — 종가가 10 · 20 · 50일선 이상인 것만, 값 100개 미만 · 음수 수익 5개 미만이면 제외
     ETR Comfort = CAGR ÷ (|MDD| × 연율 변동성) — 같은 필터
  ③ 쏠림지수 = 날마다 6개월 수익률 순위를 가운데 0인 점수(−1 ~ +1)로 바꿔 수익률에 곱한 값의 표준편차 × 100.
     35 이상이면 과열(소수 업종 쏠림), 아래면 확산.   ※ 시황 '주도 업종' 카드의 쏠림지수(WICS 업종 수급)와 다른 지표다.
  ④ Breadth = 10 · 20 · 50 · 120일선 이상인 종목 비율(유효 10개 이상), 3일 평균으로 다듬어
     종합 = 10일선 35% + 20일선 35% + 50일선 30%, 그 5일 평균과 5 · 10일 변화. 120일선 비율 < 30%면 역추세 조건.
  ⑤ 과열 점검 — 소르티노(50일선 이상) 최신 상위의 30일 이격도가 최근 247거래일 97% 구간 대비 어디인지.
  ⑥ 정배열 — 20 · 65 · 130 · 260일선(4 · 13 · 26 · 52주), 이어진 거래일.
  ⑦ 52주 신고가 전략 백테스트 — 6개월 코스피 대비 수익률 차이 상위 12 ∩ 종가 > 직전 252거래일 최고 →
     최대 4종목 × 25%(새로 사는 종목만 25%, 기존 종목은 비중이 움직이는 대로), 종가 < 10일선이면 매도, 거래비용 0.1%.
     전일 종가 신호 · 그 종가 체결 가정, 수익은 다음 거래일부터. 원본은 비중 합이 100%를 넘는 것을 막지 않는다(미국판과 다름) — 그대로 둔다.

[자료 시작일] 백테스트는 자료 시작일에 따라 달라진다. 원본 엑셀은 2025-01-21부터다(이날부터 받아야 코스피 성과와
  전략 시작일 2026-02-04가 원본과 같다). DATA_START로 고정한다.

[원본 대조 — 2026-11-17 종가, 원본 갱신 11-17 06:37 UTC]
  · 원본 엑셀은 ETF 분배금이 2026-07-30분까지만 반영돼 있다(8~10월 분배금 미반영 — 8월쯤 받은 수정 주가에 그 뒤 날짜를
    이어 붙인 것으로 보임). 그래서 운영 계산(분배금 전부 반영)은 분배금 ETF 몇 개가 원본과 6개월 수익률 0.1~1%p 다르다.
  · 같은 조건(fetch_prices(adjust_until='2026-07-31'))으로 맞추면: Mansfield RS 42개 중 41개 같음(KODEX 건설 18.28 ↔ 18.24),
    쏠림지수 22.69, Breadth 전 항목, 과열 점검, 모멘텀 순위가 같고, 백테스트도 매수 35회 · 1.67배 · 연 92.93% · Sharpe 2.15 ·
    Sortino 2.27 · 회전율 22.04배가 같다(최대 낙폭 −14.39 ↔ −14.38).
  · 야후는 2025-09-19 주식 4종목이 비어 있어(KRX로 채움) 채우지 않으면 52주 신고가가 1년간 막혀 백테스트가 크게 달라진다.

[저장] 하루 한 줄(base_date = 마지막 거래일). 지난 줄은 10일만 남긴다.

[시각] 평일 16:50(한국 장 마감 뒤) · 19:20(한 번 더). 15:45 전에는 오늘 봉을 버린다.

실행: python3 collect_kr_etf_rs.py [--dry] [--dump 경로] [--start YYYY-MM-DD]
"""

import json
import math
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client

log = get_logger(__name__)

TABLE = 'market_snapshot'
KIND = 'kr_etf'
KEEP_DAYS = 10
DATA_START = '2025-01-21'      # 원본 엑셀 시작일(코스피 비교 · 백테스트 시작 2026-02-04가 원본과 같아지는 날)
BENCH = '코스피'
BENCH_YF = '^KS11'

# 원본 엑셀 열(이름은 원본 그대로) → 야후 코드
UNIVERSE = {
    'KIWOOM 의료AI': '483020.KS', 'HANARO Fn K-푸드': '438900.KS', 'KODEX 은행': '091170.KS', 'TIGER 여행레저': '228800.KS',
    'TIGER 바이오TOP10': '364970.KS', 'TIGER 중국소비테마': '150460.KS', 'TIGER 코스피고배당': '210780.KS',
    'TIGER 200 생활소비재': '227560.KS', 'TIGER 화장품': '228790.KS', 'TIGER 소프트웨어': '157490.KS', 'KODEX K콘텐츠': '266360.KS',
    'KODEX 보험': '140700.KS', 'KODEX 건설': '117700.KS', 'SOL 화장품TOP3플러스': '0008T0.KS', 'KODEX 게임산업': '300950.KS',
    'KODEX 바이오': '244580.KS', 'TIGER 지주회사': '307520.KS', 'TIGER 2차전지테마': '305540.KS', '파마리서치': '214450.KQ',
    'TIGER 의료기기': '307510.KS', 'KODEX 에너지화학': '117460.KS', 'KODEX 자동차': '091180.KS', 'HANARO Fn친환경에너지': '381570.KS',
    'KODEX 로봇액티브': '445290.KS', '삼성전자': '005930.KS', 'SOL 반도체후공정': '475310.KS', 'PLUS 태양광&ESS': '457990.KS',
    'TIGER 코스닥150바이오테크': '261070.KS', 'KODEX 증권': '102970.KS', 'PLUS 우주항공': '421320.KS', 'SOL 반도체전공정': '475300.KS',
    'KODEX 반도체': '091160.KS', '에이피알': '278470.KS', 'RISE 네트워크인프라': '367760.KS', 'SOL 조선TOP3플러스': '466920.KS',
    'SK하이닉스': '000660.KS', 'ACE 원자력TOP10': '433500.KS', 'TIGER K방산&우주': '463250.KS', 'TIGER AI반도체핵심공정': '471760.KS',
    'KODEX AI전력핵심설비': '487240.KS', 'PLUS K방산': '449450.KS', 'TIGER 200 중공업': '139230.KS',
}
STOCK_CODES = {'삼성전자': '005930', 'SK하이닉스': '000660', '파마리서치': '214450', '에이피알': '278470'}

RS_WIN = 126
TOP_N = 20
LAST_N_DAYS = 10
CROWD_THRESHOLD = 35.0
GAP_MA, GAP_LOOKBACK, GAP_PCT = 30, 247, 0.97
MA_4W, MA_13W, MA_26W, MA_52W = 20, 65, 130, 260
BT_RS, BT_HIGH, BT_MA_EXIT, BT_TOP, BT_MAX, BT_W, BT_COST = 126, 252, 10, 12, 4, 0.25, 0.001
SERIES_KEEP = 252          # 그림용 계열은 최근 252거래일만
RECENT_TRADES = 30


def _has_schema(sb) -> bool:
    try:
        sb.table(TABLE).select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def _num(v, nd=2):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


def _krx_fill(px, codes):
    """야후에서 빠진 날(종목이 상장된 뒤인데 값이 없는 날)을 KRX 그날 전종목 종가로 채운다. 반환: 채운 칸 수."""
    import pandas as pd
    from collect_active_etf import Krx

    holes = {}
    for n in codes:
        s = px[n]
        fv = s.first_valid_index()
        if fv is None:
            continue
        for d in s.loc[fv:][s.loc[fv:].isna()].index:
            holes.setdefault(d, []).append(n)
    if not holes:
        return 0
    krx = Krx()
    krx.login()
    num = lambda v: float(v.replace(',', '')) if v not in (None, '', '-') else None
    filled = 0
    for d, names in sorted(holes.items()):
        day = d.strftime('%Y%m%d')
        etf = {r['ISU_SRT_CD']: num(r.get('TDD_CLSPRC')) for r in (krx.get(
            'dbms/MDC/STAT/standard/MDCSTAT04301', trdDd=day, share='1', money='1', csvxls_isNo='false').get('output') or [])}
        stk = {r['ISU_SRT_CD']: num(r.get('TDD_CLSPRC')) for r in (krx.get(
            'dbms/MDC/STAT/standard/MDCSTAT01501', mktId='ALL', trdDd=day, share='1', money='1', csvxls_isNo='false').get('OutBlock_1') or [])}
        for n in names:
            v = (stk if n in STOCK_CODES else etf).get(codes[n])
            if v:
                px.loc[d, n] = v
                filled += 1
    return filled


def _krx_bench(px, kst):
    """야후 코스피 지수에 빠진 거래일을 KRX 코스피 지수 종가로 채운다(없는 날이면 줄을 새로 만든다). 반환: (px, 채운 날 수).
    야후는 코스피 지수 · 일부 종목 일봉을 하루 넘게 늦게 올리는 날이 있다(2026-11-18 06시에도 11-17 봉 없음 — 일부 ETF만 있음).
    대상 = 종목 절반 이상은 값이 있는데 코스피만 빈 날 + 야후 마지막 코스피 날 뒤 평일 ~ 마지막 마감일(15:45 전이면 어제).
    휴장일은 KRX 응답에 없으므로 저절로 빠진다. 종목 칸은 뒤이은 _krx_fill이 그날 KRX 전종목 종가로 채운다."""
    import pandas as pd
    from collect_active_etf import Krx

    b = px[BENCH]
    cover = px.drop(columns=[BENCH]).notna().mean(axis=1)
    need = list(px.index[b.isna() & (cover >= 0.5)])
    last_done = kst.date() if (kst.hour, kst.minute) >= (15, 45) else kst.date() - timedelta(days=1)
    lastb = b.last_valid_index()
    d = lastb.date() + timedelta(days=1) if lastb is not None else last_done + timedelta(days=1)
    while d <= last_done:
        if d.weekday() < 5:
            need.append(pd.Timestamp(d))
        d += timedelta(days=1)
    need = sorted(set(need))
    if not need:
        return px, 0
    krx = Krx()
    krx.login()
    got = {}
    i = 0
    while i < len(need):          # KRX는 2년 넘는 기간을 400으로 거절 — 700일 안쪽 묶음으로 나눠 묻는다(10년치 빈 날이 흩어진 경우)
        j = i
        while j + 1 < len(need) and (need[j + 1] - need[i]).days <= 700:
            j += 1
        r = krx.get('dbms/MDC/STAT/standard/MDCSTAT00301', indIdx='1', indIdx2='001', strtDd=need[i].strftime('%Y%m%d'),
                    endDd=need[j].strftime('%Y%m%d'), share='2', money='3', csvxls_isNo='false')
        for x in r.get('output') or []:
            v = (x.get('CLSPRC_IDX') or '').replace(',', '')
            if x.get('TRD_DD') and v not in ('', '-'):
                got[pd.Timestamp(x['TRD_DD'].replace('/', '-'))] = float(v)
        i = j + 1
    n = 0
    for dd in need:
        if dd in got:
            px.loc[dd, BENCH] = got[dd]
            n += 1
    return px.sort_index(), n


def fetch_prices(start=DATA_START, now_utc=None, adjust_until=None):
    """야후 종가 → DataFrame(날짜 × 이름 + '코스피').
    · 거래일 = 코스피 종가가 있는 날(원본 엑셀의 DATE). 장 마감(15:45 KST) 전이면 오늘 봉은 버린다.
    · 야후 코스피가 늦게 올라온 최근 거래일 · 가운데 빈 날은 KRX 지수로 채운다(_krx_bench).
    · 야후에 빈 날은 KRX 그날 종가로 채운다(2025-09-19 주식 4종목이 비어 있어 52주 신고가가 1년간 막혔던 것).
    · ETF는 분배금을 반영한 수정 종가(분배락일 전 가격 × (1 − 분배금 ÷ 직전 종가)), 주식 4종목 · 코스피는 수정 전 종가 — 원본과 같은 조합.
      adjust_until을 주면 그날까지의 분배금만 반영한다(원본 엑셀 재현 시험용)."""
    import pandas as pd
    import yfinance as yf

    raw = yf.download(list(UNIVERSE.values()) + [BENCH_YF], start=start, auto_adjust=False, actions=True,
                      progress=False, threads=False, group_by='column')
    if raw is None or raw.empty:
        raise RuntimeError('야후 일봉을 받지 못했습니다')
    rename = {v: k for k, v in UNIVERSE.items()} | {BENCH_YF: BENCH}
    px = raw['Close'].rename(columns=rename).copy()
    px.index = pd.DatetimeIndex(px.index).tz_localize(None).normalize()
    divs = raw['Dividends'].rename(columns=rename).copy() if 'Dividends' in raw.columns.get_level_values(0) else None
    if divs is not None:
        divs.index = px.index
    kst = (now_utc or datetime.now(timezone.utc)) + timedelta(hours=9)
    px = px.sort_index()
    if len(px) and px.index[-1].date() == kst.date() and (kst.hour, kst.minute) < (15, 45):
        px = px.iloc[:-1]
    px, nb = _krx_bench(px, kst)
    if nb:
        log.info(f'[한국ETF] 야후에 없는 코스피 {nb}일을 KRX 지수로 채움')
    px = px[px[BENCH].notna()]
    cols = [n for n in UNIVERSE if n in px.columns]
    filled = _krx_fill(px, {n: UNIVERSE[n].split('.')[0] for n in cols})
    if filled:
        log.info(f'[한국ETF] 야후 빈 칸 {filled}개를 KRX 종가로 채움')
    # ETF 분배금 반영
    if divs is not None:
        cut = pd.Timestamp(adjust_until) if adjust_until else None
        for n in cols:
            if n in STOCK_CODES or n not in divs.columns:
                continue
            s = px[n]
            d = divs[n].reindex(px.index).fillna(0)
            d = d[(d > 0) & (d.index > s.first_valid_index())] if s.first_valid_index() is not None else d.iloc[0:0]
            if cut is not None:
                d = d[d.index <= cut]
            f = pd.Series(1.0, index=s.index)
            for ex, v in d.items():
                prev = s[s.index < ex].dropna()
                if not prev.empty and prev.iloc[-1] > 0:
                    f[f.index < ex] *= (1 - v / prev.iloc[-1])
            px[n] = s * f
    return px[cols + [BENCH]]


# ── 원본 7 · 8: 소르티노 · ETR ────────────────────────────────────────

def _mdd(ret):
    cum = (1 + ret.fillna(0)).cumprod()
    return float(((cum - cum.cummax()) / cum.cummax()).min())


def _sortino(ret, periods=252):
    import numpy as np
    ret = ret.dropna()
    if len(ret) < 5:
        return np.nan
    down = ret[ret < 0]
    if len(down) < 5:
        return np.nan
    dev = down.std(ddof=1) * np.sqrt(periods)
    if dev == 0 or np.isnan(dev):
        return np.nan
    return float(ret.mean() * periods / dev)


def _etr(prices, periods=252):
    import numpy as np
    prices = prices.dropna()
    if len(prices) < 60:
        return np.nan
    ret = prices.pct_change(fill_method=None).dropna()
    if len(ret) < 5:
        return np.nan
    cagr = float((prices.iloc[-1] / prices.iloc[0]) ** (periods / (len(prices) - 1)) - 1)
    vol = float(ret.std(ddof=1) * np.sqrt(periods))
    denom = abs(_mdd(ret)) * vol
    return np.nan if denom == 0 or np.isnan(denom) or np.isnan(cagr) else float(cagr / denom)


def compute(df) -> dict:
    import numpy as np
    import pandas as pd

    iso = lambda d: pd.Timestamp(d).date().isoformat()
    kospi = df[BENCH].dropna()
    data = df.drop(columns=[BENCH])
    cols = list(data.columns)

    # ① Mansfield RS
    rs_rows = []
    for c in cols:
        s = data[c].dropna()
        idx = s.index.intersection(kospi.index)
        e, k = s.loc[idx], kospi.loc[idx]
        if len(e) < RS_WIN:
            continue
        rel = e / k
        rs = (((rel / rel.rolling(RS_WIN, min_periods=RS_WIN).mean()) - 1) * 100).dropna()
        if rs.empty:
            continue
        raw = round(float(rs.iloc[-1]), 2)
        rs_rows.append({'n': c, 'rs': raw, 'norm': round(100 * (1 / (1 + np.exp(-raw / 12))), 2)})
    rs_rows.sort(key=lambda r: -r['norm'])

    ret6 = data / data.shift(RS_WIN) - 1
    k6 = kospi / kospi.shift(RS_WIN) - 1
    rel6 = ret6.sub(k6, axis=0)
    d6 = ret6.dropna(how='all').index[-1]
    six = {c: {'r6': _num(ret6.loc[d6, c] * 100), 'vs': _num(rel6.loc[d6, c] * 100)} for c in cols
           if pd.notna(ret6.loc[d6, c]) and pd.notna(rel6.loc[d6, c])}

    # ② 순위표 — 최근 10거래일 각각 상위 20
    days = data.dropna(how='all').index[-LAST_N_DAYS:]

    def ma_ok(end, ma_days):
        ok = {}
        for c in cols:
            s = data[c].iloc[:end + 1].dropna()
            ok[c] = bool(len(s) >= ma_days and s.iloc[-1] >= s.rolling(ma_days).mean().iloc[-1])
        return ok

    def rank_momentum():
        out = {}
        for d in days:
            end = data.index.get_indexer_for([d])[0]
            w = data.iloc[max(0, end - (RS_WIN - 1)):end + 1]
            r6 = w.iloc[-1] / w.iloc[0] - 1
            vol = w.pct_change(fill_method=None).std(skipna=True)
            valid = r6.notna() & vol.notna() & (vol > 0)
            sc = (r6[valid] / vol[valid]).sort_values(ascending=False).head(TOP_N)
            out[d] = {k_: _num(v, 3) for k_, v in sc.items()}
        return out

    def rank_metric(metric, ma_days):
        out = {}
        for d in days:
            end = data.index.get_indexer_for([d])[0]
            ok = ma_ok(end, ma_days)
            scores = {}
            for c in cols:
                if not ok.get(c):
                    continue
                w = data[c].iloc[max(0, end - (RS_WIN - 1)):end + 1].dropna()
                if len(w) < max(20, int(RS_WIN * 0.8)):
                    continue
                sc = _sortino(w.pct_change(fill_method=None).dropna()) if metric == 'sortino' else _etr(w)
                if pd.notna(sc):
                    scores[c] = float(sc)
            ranked = pd.Series(scores, dtype=float).sort_values(ascending=False).head(TOP_N)
            out[d] = {k_: _num(v, 3) for k_, v in ranked.items()}
        return out

    def pack(o):
        ds = sorted(o)
        return {'dates': [iso(d) for d in ds], 'ranks': [list(o[d]) for d in ds], 'scores': o[ds[-1]] if ds else {}}

    lists = {'mom': pack(rank_momentum())}
    for m in ('sortino', 'etr'):
        for ma in (10, 20, 50):
            lists[f"{'sor' if m == 'sortino' else 'etr'}{ma}"] = pack(rank_metric(m, ma))

    # ③ 쏠림지수
    r6all = data.pct_change(RS_WIN, fill_method=None)
    crowd = {}
    for d in r6all.index:
        r = r6all.loc[d].dropna()
        n = len(r)
        if n < 10:
            continue
        rank = r.rank(ascending=False, method='average')
        score = ((n + 1) / 2 - rank) / ((n - 1) / 2)
        crowd[d] = float((score * r).std(ddof=1) * 100)
    crowd = pd.Series(crowd).sort_index()
    cdate = crowd.index[-1]
    r_last = r6all.loc[cdate].dropna().sort_values(ascending=False)

    # ④ Breadth
    br = pd.DataFrame(index=data.index)
    for w in (10, 20, 50, 120):
        ma = data.rolling(w, min_periods=w).mean()
        valid = data.notna() & ma.notna()
        vc = valid.sum(axis=1)
        ac = ((data >= ma) & valid).sum(axis=1)
        ratio = (ac / vc.replace(0, np.nan) * 100).where(vc >= 10)
        br[f'a{w}'] = ratio
        br[f'c{w}'] = ac
        br[f'v{w}'] = vc
        br[f's{w}'] = ratio.rolling(3, min_periods=1).mean()
    br['score'] = br['s10'] * 0.35 + br['s20'] * 0.35 + br['s50'] * 0.30
    br['ma5'] = br['score'].rolling(5, min_periods=1).mean()
    br['sl5'] = br['ma5'] - br['ma5'].shift(5)
    br['sl10'] = br['ma5'] - br['ma5'].shift(10)
    bv = br.dropna(subset=['ma5'])
    bl = bv.iloc[-1]
    ma120 = float(bl['a120']) if pd.notna(bl['a120']) else None
    rev = None if ma120 is None else ('met' if ma120 < 30 else 'near' if ma120 < 40 else 'wait')

    # ⑤ 과열 점검 — 소르티노(50일선 이상) 최신 상위
    gap_rows = []
    for rank_no, c in enumerate(lists['sor50']['ranks'][-1] if lists['sor50']['ranks'] else [], start=1):
        price = data[c].dropna()
        if len(price) < GAP_MA + 5:
            continue
        ma30 = price.rolling(GAP_MA).mean()
        gap = (price / ma30 * 100).dropna()
        if gap.empty:
            continue
        recent = gap.tail(GAP_LOOKBACK)
        cur = float(gap.iloc[-1]) - 100
        p97 = float(recent.quantile(GAP_PCT)) - 100
        gap_rows.append({'rank': rank_no, 'n': c, 'close': _num(price.loc[gap.index[-1]], 0), 'ma30': _num(ma30.loc[gap.index[-1]], 0),
                         'gap': _num(cur), 'p97': _num(p97), 'heat': _num(cur / p97 * 100, 1) if p97 != 0 else None,
                         'date': iso(gap.index[-1]),
                         'series': {'dates': [iso(d) for d in recent.index], 'price': [_num(v, 0) for v in price.reindex(recent.index)],
                                    'gaps': [_num(v - 100, 2) for v in recent]}})

    # ⑥ 정배열
    mas = {n: data.rolling(n, min_periods=n).mean() for n in (MA_4W, MA_13W, MA_26W, MA_52W)}

    def streak(mask):
        mask = mask.dropna().astype(bool)
        if mask.empty or not bool(mask.iloc[-1]):
            return 0, None
        fp = np.flatnonzero(~mask.to_numpy())
        st = 0 if len(fp) == 0 else fp[-1] + 1
        return int(len(mask) - st), iso(mask.index[st])

    align = []
    for c in cols:
        t = pd.DataFrame({'p': data[c], 'a': mas[MA_4W][c], 'b': mas[MA_13W][c], 'c': mas[MA_26W][c], 'd': mas[MA_52W][c]}).dropna()
        if t.empty:
            continue
        mm = (t['a'] > t['b']) & (t['b'] > t['c']) & (t['c'] > t['d'])
        fm = (t['p'] > t['a']) & mm
        r = t.iloc[-1]
        md, ms = streak(mm)
        fd, fs = streak(fm)
        gaps = [(r['p'] / r['a'] - 1) * 100, (r['a'] / r['b'] - 1) * 100, (r['b'] / r['c'] - 1) * 100, (r['c'] / r['d'] - 1) * 100]
        align.append({'n': c, 'date': iso(t.index[-1]), 'ma': bool(mm.iloc[-1]), 'ma_start': ms, 'ma_days': md,
                      'full': bool(fm.iloc[-1]), 'full_start': fs, 'full_days': fd, 'above4w': bool(r['p'] > r['a']),
                      'gaps': [_num(g) for g in gaps], 'strength': _num(sum(gaps))})
    align.sort(key=lambda a: (not a['full'], -a['full_days'], -(a['strength'] or -1e9)))

    bt = backtest(data, kospi)

    def tail_series(s, nd=2):
        s = s.dropna().tail(SERIES_KEEP)
        return {'dates': [iso(d) for d in s.index], 'v': [_num(v, nd) for v in s]}

    return {
        'asof': iso(data.dropna(how='all').index[-1]), 'start': iso(df.index[0]),
        'n': len(cols), 'kospi': {'last': _num(kospi.iloc[-1], 2), 'r6': _num(k6.loc[d6] * 100) if pd.notna(k6.loc[d6]) else None},
        'codes': {n: STOCK_CODES.get(n) for n in cols if n in STOCK_CODES},
        'rs': rs_rows, 'six': six, 'six_date': iso(d6),
        'lists': lists,
        'crowd': {'date': iso(cdate), 'value': _num(crowd.iloc[-1], 2), 'threshold': CROWD_THRESHOLD,
                  'series': tail_series(crowd),
                  'leaders': [{'n': c, 'r6': _num(v * 100)} for c, v in r_last.head(10).items()],
                  'laggards': [{'n': c, 'r6': _num(v * 100)} for c, v in r_last.tail(10).sort_values().items()]},
        'breadth': {'date': iso(bv.index[-1]),
                    'now': {k_: _num(bl[k_], 1) for k_ in ('a10', 'a20', 'a50', 'a120', 'ma5', 'sl5', 'sl10')},
                    'count': {f'ma{w}': [int(bl[f'c{w}']), int(bl[f'v{w}'])] for w in (10, 20, 50, 120)},
                    'reverse': rev,
                    'series': {'dates': [iso(d) for d in bv.index[-SERIES_KEEP:]],
                               **{k_: [_num(v, 1) for v in bv[k_].tail(SERIES_KEEP)] for k_ in ('a10', 'a20', 'a50', 'a120', 'ma5')}},
                    'recent': [{'d': iso(d), **{k_: _num(r_[k_], 1) for k_ in ('a10', 'a20', 'a50', 'a120', 'ma5', 'sl5', 'sl10')}}
                               for d, r_ in bv.tail(20).iterrows()]},
        'gap': gap_rows, 'align': align, 'bt': bt,
    }


def backtest(data, kospi) -> dict:
    """원본 13. 최종 52주 신고가 전략 — 6M 코스피 대비 RS 상위 12 ∩ 52주 돌파, 10일선 이탈 매도, 최대 4 × 25%."""
    import numpy as np
    import pandas as pd

    iso = lambda d: pd.Timestamp(d).date().isoformat()
    bp, bk = data.sort_index(), kospi.sort_index()
    idx = bp.index.intersection(bk.index)
    bp, bk = bp.loc[idx].dropna(axis=1, how='all'), bk.loc[idx]
    ret = bp.pct_change(fill_method=None)
    kret = bk.pct_change(fill_method=None)
    rs6 = (bp / bp.shift(BT_RS) - 1).sub(bk / bk.shift(BT_RS) - 1, axis=0)
    prev_high = bp.shift(1).rolling(BT_HIGH, min_periods=BT_HIGH).max()
    brk = bp > prev_high
    ma10 = bp.rolling(BT_MA_EXIT, min_periods=BT_MA_EXIT).mean()
    below = bp < ma10
    dates, cols = bp.index, list(bp.columns)
    N, T = len(cols), len(dates)
    P, RS, BRK, BEL, M10, PH = bp.to_numpy(), rs6.to_numpy(), brk.to_numpy(), below.to_numpy(), ma10.to_numpy(), prev_high.to_numpy()
    R = ret.reindex(columns=cols).fillna(0.0).to_numpy()

    equity, turnover = np.ones(T), np.zeros(T)
    w = np.zeros(N)
    trades, held_rows = [], []
    first = max(BT_RS, BT_HIGH, BT_MA_EXIT)
    for i in range(1, T):
        s = i - 1
        prev_eq = equity[i - 1]
        if s >= first:
            target = w.copy()
            holdings = [j for j in range(N) if w[j] > 0]
            sells = [j for j in holdings if bool(BEL[s, j])]
            for j in sells:
                trades.append({'sd': iso(dates[s]), 't': cols[j], 'a': 'SELL', 'close': _num(P[s, j], 0), 'ma10': _num(M10[s, j], 1)})
                target[j] = 0.0
            remaining = [j for j in range(N) if target[j] > 0]
            rs_today = pd.Series(RS[s], index=range(N)).dropna().sort_values(ascending=False)
            rank_map = {j: r for r, j in enumerate(rs_today.index, start=1)}
            cands = [j for j in rs_today.head(BT_TOP).index if j not in remaining and bool(BRK[s, j])]
            cands = sorted(cands, key=lambda j: rank_map.get(j, 999))
            for j in cands[:max(0, BT_MAX - len(remaining))]:
                target[j] = BT_W
                trades.append({'sd': iso(dates[s]), 't': cols[j], 'a': 'BUY', 'close': _num(P[s, j], 0), 'ma10': _num(M10[s, j], 1),
                               'high': _num(PH[s, j], 0), 'rk': rank_map.get(j), 'rs': _num(RS[s, j] * 100)})
            to = float(np.abs(target - w).sum())
            if to > 0:
                prev_eq *= max(0.0, 1.0 - BT_COST * to)
            turnover[i] = to
            w = target
        r = R[i]
        mult = max(0.0, 1.0 - float(w.sum())) + float((w * (1 + r)).sum())
        equity[i] = prev_eq * mult
        if mult > 0:
            w = w * (1 + r) / mult
        held = [(cols[j], float(w[j])) for j in np.argsort(-w) if w[j] > 0]
        held_rows.append({'d': iso(dates[i]), 'n': len(held), 'inv': float(sum(x for _, x in held)),
                          'h': [[t, _num(x * 100, 1)] for t, x in held]})

    eq = pd.Series(equity, index=dates)
    if not trades:
        return {'start': None, 'trades': [], 'note': '거래가 아직 없습니다'}
    start = pd.Timestamp(min(t['sd'] for t in trades))
    pos = eq.index.get_loc(start)

    def norm(curve):
        return curve.loc[start:] / (curve.iloc[pos - 1] if pos > 0 else curve.iloc[pos])

    s_live = norm(eq)
    k_live = norm((1 + kret.fillna(0)).cumprod())
    to_ser = pd.Series(turnover, index=dates)

    def stats(curve, tov=None):
        curve = curve.dropna()
        years = (curve.index[-1] - curve.index[0]).days / 365.25
        if years <= 0:
            years = len(curve) / 252
        cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
        dd = (curve / curve.cummax() - 1).min()
        r = curve.pct_change().dropna()
        sd = r.std(ddof=1)
        sharpe = r.mean() / sd * np.sqrt(252) if pd.notna(sd) and sd > 0 else np.nan
        dsd = r[r < 0].std(ddof=1)
        sortino = r.mean() / dsd * np.sqrt(252) if pd.notna(dsd) and dsd > 0 else np.nan
        at = tov.reindex(curve.index).fillna(0).sum() / years if tov is not None and years > 0 else None
        return {'cagr': _num(cagr * 100), 'mdd': _num(dd * 100), 'sharpe': _num(sharpe), 'sortino': _num(sortino),
                'turnover': _num(at), 'mult': _num(curve.iloc[-1] / curve.iloc[0], 4)}

    def yearly(curve):
        r = curve.pct_change().fillna(0)
        return (1 + r).groupby(r.index.year).prod() - 1

    ys, yk = yearly(s_live), yearly(k_live)
    live_h = [h for h in held_rows if h['d'] >= iso(start)]
    last_d = dates[-1]
    rs_last = rs6.loc[last_d].dropna().sort_values(ascending=False)
    top12 = [{'rank': r_, 'n': c, 'close': _num(bp.loc[last_d, c], 0), 'high': _num(prev_high.loc[last_d, c], 0),
              'brk': bool(brk.loc[last_d, c]) if pd.notna(brk.loc[last_d, c]) else False, 'rs': _num(rs_last[c] * 100)}
             for r_, c in enumerate(rs_last.head(BT_TOP).index, start=1)]
    cur = held_rows[-1] if held_rows else {'h': [], 'inv': 0}
    cur_hold = [{'n': t, 'w': x, 'ma_gap': _num((bp.loc[last_d, t] / ma10.loc[last_d, t] - 1) * 100) if pd.notna(ma10.loc[last_d, t]) else None}
                for t, x in cur['h']]
    return {
        'start': iso(start),
        'perf': {'strategy': stats(s_live, to_ser), 'kospi': stats(k_live)},
        'yearly': {'years': [int(y) for y in ys.index], 'strategy': [_num(v * 100) for v in ys], 'kospi': [_num(yk.get(y, np.nan) * 100) for y in ys.index]},
        'trade_stats': {'buys': sum(1 for t in trades if t['a'] == 'BUY'), 'sells': sum(1 for t in trades if t['a'] == 'SELL'),
                        'avg_hold': _num(np.mean([h['n'] for h in live_h]) if live_h else None, 2),
                        'max_hold': max((h['n'] for h in live_h), default=0),
                        'avg_inv': _num(np.mean([h['inv'] for h in live_h]) * 100 if live_h else None, 1)},
        'holdings': cur_hold, 'invested': _num(cur['inv'] * 100, 1),
        'curve': {'dates': [iso(d) for d in s_live.index], 'strategy': [_num(v, 4) for v in s_live], 'kospi': [_num(v, 4) for v in k_live]},
        'trades': trades[-RECENT_TRADES:],
        'top12': top12,
        'today_sell': [h['n'] for h in cur_hold if h['ma_gap'] is not None and h['ma_gap'] < 0],
    }


def run(dry: bool = False, start: str = DATA_START) -> dict:
    sb = get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[한국ETF] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    df = fetch_prices(start)
    missing = [n for n in UNIVERSE if n not in df.columns or df[n].notna().sum() == 0]
    if len(missing) > len(UNIVERSE) * 0.2 or len(df) < 300:
        raise RuntimeError(f'야후 수신 부족 — 빠진 종목 {len(missing)}개, {len(df)}일')
    p = compute(df)
    p['missing'] = missing
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': p['asof'], 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
        cutoff = (datetime.fromisoformat(p['asof']) - timedelta(days=KEEP_DAYS)).date().isoformat()
        sb.table(TABLE).delete().eq('kind', KIND).lt('base_date', cutoff).execute()
    bt = p['bt']
    log.info(f"[한국ETF] {'dry-run ' if dry else ''}{p['asof']} 종목 {p['n']} · 쏠림 {p['crowd']['value']} · Breadth {p['breadth']['now']['ma5']} · "
             f"RS 70 이상 {sum(1 for r in p['rs'] if (r['norm'] or 0) >= 70)}개 · 전략 {bt.get('start')}~ "
             f"{(bt.get('perf') or {}).get('strategy', {}).get('mult')}배 · 보유 {len(bt.get('holdings') or [])}"
             f"{' · 빠짐 ' + ', '.join(missing) if missing else ''}")
    return {'date': p['asof'], 'payload': p}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    ap.add_argument('--dump', help='계산 결과를 이 경로에 JSON으로 저장')
    ap.add_argument('--start', default=DATA_START, help='자료 시작일(백테스트가 이 날부터 252거래일 뒤 시작)')
    a = ap.parse_args()
    r = run(dry=a.dry, start=a.start)
    if a.dump and r.get('payload'):
        with open(a.dump, 'w', encoding='utf-8') as f:
            json.dump(r['payload'], f, ensure_ascii=False)
    print({k: v for k, v in r.items() if k != 'payload'})
