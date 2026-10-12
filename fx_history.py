"""
fx_history.py
─────────────
원화 환율 이력 — 외화로 공시하는 상장사의 DART 재무제표를 원화로 바꿀 때 쓴다 (collect_financials, 2026-10-12)

외화 공시 상장사(2026-10 조사, DART 다중회사 주요계정의 currency): 23곳
  USD — 두산밥캣(2023~)·엑세스바이오·네오이뮨텍·소마젠·코오롱티슈진·잉글우드랩·아남전자·고스트스튜디오(2022~) 등
  CNY — 크리스탈신소재·로스웰·컬러레이 등 중국계 9곳 (글로벌에스엠은 2024년부터 USD)
  JPY — JTC        HKD — 고스트스튜디오 2021
연도마다 통화가 바뀔 수 있어(두산밥캣 2022 KRW → 2023 USD) 보고서마다 DART 응답의 currency를 본다.

환산 규칙(사용자 결정 10-12, 회계기준 IAS 21 외화환산과 같음):
  - 손익·현금흐름: 그 금액이 덮는 기간의 평균 환율 (분기 단독 = 분기 평균, 누적 = 1월 1일부터 평균, 연간 = 연 평균)
  - 재무상태표: 기말 환율 (기간 마지막 날 이전 마지막 거래일 종가)
  → 분기 단독 = 누적 원화 − 직전 누적 원화라서 네 분기 합이 연간 원화 금액과 맞는다.

출처: yfinance 일별 종가 (macro_data 환율은 2026-02부터라 과거 분기에 못 씀).
  1통화 = N원. USD는 KRW=X, 나머지는 KRW=X ÷ {통화}=X(1달러당 그 통화)를 같은 날짜끼리.
  data/fx_daily.json에 캐시하고, 필요한 기간이 캐시 끝보다 뒤면 다시 받는다.
"""
import json
import os
from datetime import date, datetime, timedelta

from logger_config import get_logger
log = get_logger(__name__)

CACHE_PATH = '/home/kjhofone/data/fx_daily.json'
HISTORY_START = '2015-01-01'

_mem = {}   # {통화: {'YYYY-MM-DD': 원}}
_fetched = set()


def _download(ccy: str) -> dict:
    import yfinance as yf

    def closes(ticker):
        h = yf.Ticker(ticker).history(start=HISTORY_START, end=(date.today() + timedelta(days=1)).isoformat(),
                                      interval='1d')['Close'].dropna()
        h.index = h.index.tz_localize(None).normalize()
        # 엉뚱한 틱(앞뒤 11일 중앙값과 10% 넘게 차이) 버림 — 2015~2026 실측으론 최대 3%라 실제로 걸리는 값은 없다
        med = h.rolling(11, center=True, min_periods=3).median()
        h = h[(h / med - 1).abs() <= 0.10]
        return {d.strftime('%Y-%m-%d'): float(v) for d, v in h.items()}

    usd = closes('KRW=X')
    if ccy == 'USD':
        return usd
    per_usd = closes(f'{ccy}=X')
    return {d: usd[d] / v for d, v in per_usd.items() if d in usd and v}


def _series(ccy: str, need_until: str) -> dict:
    """ccy 일별 원화 환율. need_until(YYYY-MM-DD)까지 캐시에 없으면(오늘 이전 한도) 다시 받는다."""
    if ccy not in _mem:
        try:
            _mem.update(json.load(open(CACHE_PATH)))
        except Exception:
            pass
    s = _mem.get(ccy) or {}
    last = max(s) if s else ''
    want = min(need_until, (date.today() - timedelta(days=1)).isoformat())
    if last < want and ccy not in _fetched:     # 한 프로세스에서 통화당 한 번만 받는다(주말엔 캐시 끝 < 어제)
        _fetched.add(ccy)
        fresh = _download(ccy)
        if not fresh:
            raise RuntimeError(f'{ccy} 환율 이력을 받지 못함')
        s = _mem[ccy] = fresh
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            tmp = CACHE_PATH + '.tmp'
            json.dump(_mem, open(tmp, 'w'))
            os.replace(tmp, CACHE_PATH)
        except Exception as e:
            log.warning(f'[환율] 캐시 저장 실패: {e}')
    return s


def avg_rate(ccy: str, start: date, end: date) -> float:
    """start~end(포함) 거래일 종가 평균 — 1통화 = N원."""
    s = _series(ccy, end.isoformat())
    a, b = start.isoformat(), end.isoformat()
    vals = [v for d, v in s.items() if a <= d <= b]
    if not vals:
        raise RuntimeError(f'{ccy} 환율 없음 {a}~{b}')
    return sum(vals) / len(vals)


def close_rate(ccy: str, on: date) -> float:
    """on 이전(포함) 마지막 거래일 종가 — 1통화 = N원. 7일 안에 값이 없으면 오류."""
    s = _series(ccy, on.isoformat())
    b, a = on.isoformat(), (on - timedelta(days=7)).isoformat()
    days = [d for d in s if a <= d <= b]
    if not days:
        raise RuntimeError(f'{ccy} 기말 환율 없음 {b}')
    return s[max(days)]


def quarter_bounds(year, quarter):
    """(분기 첫날, 연초, 분기 말일) — 12월 결산 기준."""
    y, n = int(year), int(str(quarter).upper().lstrip('Q'))
    end = (date(y + 1, 1, 1) if n == 4 else date(y, 3 * n + 1, 1)) - timedelta(days=1)
    return date(y, 3 * n - 2, 1), date(y, 1, 1), end


if __name__ == '__main__':
    import sys
    ccy = (sys.argv[1] if len(sys.argv) > 1 else 'USD').upper()
    for y in range(2021, datetime.now().year + 1):
        for q in ('Q1', 'Q2', 'Q3', 'Q4'):
            qs, ys, qe = quarter_bounds(y, q)
            if qe >= date.today():
                break
            print(f'{y} {q}  분기평균 {avg_rate(ccy, qs, qe):9.3f}  누적평균 {avg_rate(ccy, ys, qe):9.3f}  기말 {close_rate(ccy, qe):9.3f}')
