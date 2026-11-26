"""
us_calendar.py — 미국 증시(NYSE) 휴장일 · '방금 끝난 뉴욕 세션' 날짜 (표준 라이브러리만)
──────────────────────────────────────────────────────────────────────────────
tests/test_us_calendar.py. job_infra(_job의 us_holiday 가드)·collect_us_index·managers가 쓴다.

왜 필요한가 (2026-11-26 추수감사절 실측):
  · 11-27 06:50 · 08:10 job_us_index가 '2026-11-26 S&P500·NASDAQ 종가가 비었습니다'로 실패해 실패 알림이 나갔다.
    야후 ^VIX는 Cboe 연장 거래로 휴장일에도 봉이 생긴다(11-26 26.80, 05-25 · 09-07도). 여러 티커를 한꺼번에
    받으면 그날 행이 생기고 S&P500 · NASDAQ 칸만 빈다. 다음 거래일엔 그 행이 '새 날'로 저장됐다(05-25 · 09-07).
  · SPY · QQQ 같은 미국 현물에는 휴장일 봉이 없다. 그래도 '야후 일봉에 그날 행이 있는지'만으로는
    휴장과 '야후가 늦은 날'을 가르지 못한다(늦은 날은 실패 알림이 맞다) → 휴장일은 규칙으로 계산한다.
  · managers.MarketTimer.is_us_holiday()의 고정 목록은 2026년까지만 있었다.

NYSE 정규 휴장일(NYSE Rule 7.2) — 해마다 같은 규칙:
  신정 1/1 · 마틴 루서 킹 1월 셋째 월 · 대통령의 날 2월 셋째 월 · 성금요일(부활절 2일 전) ·
  메모리얼 데이 5월 마지막 월 · 준틴스 6/19(2022년부터) · 독립기념일 7/4 · 노동절 9월 첫째 월 ·
  추수감사절 11월 넷째 목 · 크리스마스 12/25.
  날짜가 정해진 휴일이 토요일이면 전 금요일, 일요일이면 다음 월요일에 쉰다.
  단 신정이 토요일이면 쉬지 않는다(전 금요일이 연말 결산일 — 2022-01-01 · 2028-01-01).
  조기 폐장(추수감사절 다음 날 · 크리스마스이브 등 13:00 마감)은 거래일이다.
  임시 휴장(국장 등)은 미리 알 수 없어 SPECIAL_CLOSURES에 적는다 — 빠뜨리면 그날을 거래일로 보고
  job_us_index가 실패 알림을 보낸다(알아차릴 수 있는 쪽으로 틀린다).
"""
import datetime as _dt
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
SESSION_DONE = (16, 15)    # 뉴욕 이 시각부터 그날 정규장(16:00 마감)을 끝난 것으로 본다 — collect_* 의 NY_CLOSE와 같다

# 정규 규칙 밖의 임시 휴장
SPECIAL_CLOSURES = frozenset({
    _dt.date(2018, 12, 5),     # 조지 H. W. 부시 전 대통령 국장
    _dt.date(2025, 1, 9),      # 지미 카터 전 대통령 국장
})


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> _dt.date:
    """그달 n번째 요일(weekday: 월=0)."""
    first = _dt.date(year, month, 1)
    return first + _dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> _dt.date:
    """그달 마지막 요일."""
    nxt = _dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - _dt.timedelta(days=1)
    return last - _dt.timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> _dt.date:
    """그레고리력 부활절(익명 알고리즘 · Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return _dt.date(year, month, day)


def _observed(d: _dt.date) -> _dt.date:
    """토요일 → 전 금요일, 일요일 → 다음 월요일."""
    if d.weekday() == 5:
        return d - _dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + _dt.timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset:
    """그해 NYSE 종일 휴장일(평일만). 임시 휴장 포함."""
    days = set()
    ny = _dt.date(year, 1, 1)
    if ny.weekday() != 5:                       # 토요일 신정은 쉬지 않는다
        days.add(_observed(ny))
    days.add(_nth_weekday(year, 1, 0, 3))       # 마틴 루서 킹
    days.add(_nth_weekday(year, 2, 0, 3))       # 대통령의 날
    days.add(_easter(year) - _dt.timedelta(days=2))   # 성금요일
    days.add(_last_weekday(year, 5, 0))         # 메모리얼 데이
    if year >= 2022:
        days.add(_observed(_dt.date(year, 6, 19)))    # 준틴스
    days.add(_observed(_dt.date(year, 7, 4)))   # 독립기념일
    days.add(_nth_weekday(year, 9, 0, 1))       # 노동절
    days.add(_nth_weekday(year, 11, 3, 4))      # 추수감사절
    days.add(_observed(_dt.date(year, 12, 25)))  # 크리스마스
    days |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return frozenset(d for d in days if d.year == year and d.weekday() < 5)


def _as_date(d) -> _dt.date:
    if isinstance(d, _dt.datetime):
        return d.date()
    if isinstance(d, _dt.date):
        return d
    return _dt.date.fromisoformat(str(d)[:10])


def is_holiday(d) -> bool:
    """평일인데 NYSE가 쉬는 날."""
    d = _as_date(d)
    return d.weekday() < 5 and d in nyse_holidays(d.year)


def is_trading_day(d) -> bool:
    """NYSE 정규장이 열리는 날(조기 폐장 포함). d: date · datetime · 'YYYY-MM-DD'."""
    d = _as_date(d)
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def session_date(now: _dt.datetime = None) -> _dt.date:
    """지금 기준으로 정규장이 끝났어야 할 가장 최근 뉴욕 날짜 — 주말 · 휴장일일 수도 있다.
    뉴욕 16:15 전이면 전날. 한국 아침(06:30~08:50) · 19:25 재처리 · 19:50 운영 요약은 모두 뉴욕 전날이다.
    now: 시간대가 있는 datetime(없으면 지금)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    ny = now.astimezone(NY)
    d = ny.date()
    return d if (ny.hour, ny.minute) >= SESSION_DONE else d - _dt.timedelta(days=1)


def holiday_session(now: _dt.datetime = None):
    """방금 끝났어야 할 뉴욕 세션이 평일 휴장일이면 그 날짜, 아니면 None.
    주말은 None — 주말 실행은 지금처럼 마지막 거래일 값을 다시 계산한다(같은 기준일 덮어씀)."""
    d = session_date(now)
    return d if is_holiday(d) else None
