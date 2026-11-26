"""us_calendar — NYSE 휴장일 규칙 · '방금 끝난 뉴욕 세션' (순수 모듈) + 그것을 쓰는 두 곳.

지키려는 것(2026-11-26 추수감사절 사고):
  · 휴장일 다음 아침 미국 잡이 실패 알림 없이 '미국 휴장 — 건너뜀'으로 끝나고, 19:50 운영 요약도 미실행으로 보지 않는다.
  · ^VIX 연장 거래 봉만 있는 휴장일 행(11-26 · 05-25 · 09-07)이 us_index_daily에 들어가지 않는다.
  · 거래일(조기 폐장 포함)은 절대 휴장으로 보지 않는다 — 그쪽으로 틀리면 그날 자료를 잃는다."""
import datetime as dt
import sys
import types
from zoneinfo import ZoneInfo

import pytest

import us_calendar as uc

KST = ZoneInfo('Asia/Seoul')
D = dt.date


def _kst(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm, tzinfo=KST)


# ── 휴장일 규칙 ───────────────────────────────────────────────────────

# managers.MarketTimer.is_us_holiday()가 2026-11까지 쓰던 고정 목록 그대로(규칙이 이걸 재현해야 한다)
OLD_STATIC = {
    2025: ['20250101', '20250120', '20250217', '20250418', '20250526', '20250619', '20250704', '20250901', '20251127', '20251225'],
    2026: ['20260101', '20260119', '20260216', '20260403', '20260525', '20260619', '20260703', '20260907', '20261126', '20261225'],
}
# NYSE 공표 일정(관측일 규칙이 걸리는 해)
PUBLISHED = {
    2022: ['2022-01-17', '2022-02-21', '2022-04-15', '2022-05-30', '2022-06-20', '2022-07-04', '2022-09-05', '2022-11-24', '2022-12-26'],
    2027: ['2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24'],
    2028: ['2028-01-17', '2028-02-21', '2028-04-14', '2028-05-29', '2028-06-19', '2028-07-04', '2028-09-04', '2028-11-23', '2028-12-25'],
}


@pytest.mark.parametrize('year', sorted(OLD_STATIC))
def test_rules_match_old_static_list(year):
    want = {dt.datetime.strptime(s, '%Y%m%d').date() for s in OLD_STATIC[year]}
    assert uc.nyse_holidays(year) - uc.SPECIAL_CLOSURES == want


@pytest.mark.parametrize('year', sorted(PUBLISHED))
def test_rules_match_published_schedule(year):
    assert uc.nyse_holidays(year) == {D.fromisoformat(s) for s in PUBLISHED[year]}


def test_saturday_new_year_is_not_moved_to_friday():
    # 2022-01-01 · 2028-01-01은 토요일 — 전 금요일(연말 결산일)에 쉬지 않는다
    assert uc.is_trading_day('2021-12-31') and uc.is_trading_day('2027-12-31')


def test_special_closure():
    assert uc.is_holiday('2025-01-09') and not uc.is_trading_day('2025-01-09')


@pytest.mark.parametrize('year,easter', [(2019, '2019-04-21'), (2024, '2024-03-31'), (2025, '2025-04-20'),
                                         (2026, '2026-04-05'), (2027, '2027-03-28'), (2028, '2028-04-16')])
def test_easter(year, easter):
    assert uc._easter(year) == D.fromisoformat(easter)


def test_trading_day_inputs_and_half_days():
    assert not uc.is_trading_day('2026-11-26')          # 추수감사절
    assert uc.is_trading_day('2026-11-27')              # 다음 날 13:00 조기 폐장 — 거래일
    assert uc.is_trading_day(D(2026, 12, 24))           # 크리스마스이브 조기 폐장 — 거래일
    assert not uc.is_trading_day(dt.datetime(2026, 12, 25, 9, 0))
    assert not uc.is_trading_day('2026-11-28')          # 토요일
    assert not uc.is_holiday('2026-11-28')              # 주말은 '휴장일'이 아니다(가드는 주말을 통과시킨다)
    assert uc.is_trading_day('2026-10-12') and uc.is_trading_day('2026-11-11')   # 채권만 쉬는 날은 거래일


# ── 방금 끝난 뉴욕 세션 ───────────────────────────────────────────────

@pytest.mark.parametrize('now,session,holiday', [
    (_kst(2026, 11, 27, 6, 50), '2026-11-26', True),    # 06:50 job_us_index (뉴욕 11-26 16:50)
    (_kst(2026, 11, 27, 8, 50), '2026-11-26', True),
    (_kst(2026, 11, 27, 16, 20), '2026-11-26', True),   # 16:20 US ETF 재수집 (뉴욕 11-27 02:20 — 아직 장 전)
    (_kst(2026, 11, 27, 19, 25), '2026-11-26', True),   # 19:25 재처리 · 19:50 운영 요약도 같은 날
    (_kst(2026, 11, 28, 6, 50), '2026-11-27', False),   # 토요일 아침 — 금요일(조기 폐장) 세션
    (_kst(2026, 11, 29, 7, 0), '2026-11-28', False),    # 일요일 아침 — 뉴욕 토요일(주말은 가드 통과)
    (_kst(2026, 11, 30, 7, 0), '2026-11-29', False),    # 월요일 아침 — 뉴욕 일요일
    (_kst(2026, 12, 26, 7, 0), '2026-12-25', True),     # 다음 휴장일: 크리스마스(금) → 한국 토요일 아침
    (_kst(2026, 12, 25, 7, 0), '2026-12-24', False),    # 한국 크리스마스 아침 = 뉴욕 12-24(조기 폐장) — 돈다
    (_kst(2026, 7, 4, 6, 30), '2026-07-03', True),      # 여름(EDT): 독립기념일 대체 휴장(금)
    (_kst(2026, 7, 3, 6, 30), '2026-07-02', False),
    (_kst(2026, 11, 26, 5, 0), '2026-11-24', False),    # 뉴욕 11-25 15:00 — 아직 장중이면 전날
])
def test_session_date(now, session, holiday):
    assert uc.session_date(now) == D.fromisoformat(session)
    assert (uc.holiday_session(now) is not None) is holiday


# ── collect_us_index.build_rows — 휴장일 행 버림 ───────────────────────

def test_build_rows_drops_holiday_vix_only_row():
    pd = pytest.importorskip('pandas')
    import collect_us_index as cui

    nan = float('nan')
    syms = list(cui.SYMS.values())
    vals = {'2026-11-24': 7201.09, '2026-11-25': 7182.78, '2026-11-26': nan, '2026-11-27': 7210.0}
    close = pd.DataFrame({s: [v if s in ('^GSPC', '^IXIC', 'QQQ') else 1.0 for v in vals.values()] for s in syms},
                         index=pd.to_datetime(list(vals)))
    close.loc[pd.Timestamp('2026-11-26'), [s for s in syms if s != '^VIX']] = nan     # 11-26: ^VIX만 있음
    ny = dt.datetime(2026, 11, 26, 17, 0, tzinfo=uc.NY)                               # 11-27 아침 실행
    rows, off = cui.build_rows(close.loc[:'2026-11-26'], ny)
    assert [r['base_date'] for r in rows] == ['2026-11-24', '2026-11-25'] and off == ['2026-11-26']
    assert rows[-1]['spx'] == 7182.78                                                 # → 마지막 행 검사 통과(실패 아님)
    ny = dt.datetime(2026, 11, 27, 17, 0, tzinfo=uc.NY)                               # 다음 거래일 — 예전엔 11-26 행을 새로 저장
    rows, off = cui.build_rows(close, ny)
    assert [r['base_date'] for r in rows] == ['2026-11-24', '2026-11-25', '2026-11-27'] and off == ['2026-11-26']


def test_build_rows_keeps_trading_day_with_missing_close():
    """거래일에 S&P500이 비면 행을 남겨 둔다 — fetch()가 '종가가 비었습니다'로 실패해 알린다(야후 늦은 날)."""
    pd = pytest.importorskip('pandas')
    import collect_us_index as cui

    syms = list(cui.SYMS.values())
    close = pd.DataFrame({s: [1.0, 1.0] for s in syms}, index=pd.to_datetime(['2026-11-24', '2026-11-25']))
    close.loc[pd.Timestamp('2026-11-25'), '^GSPC'] = float('nan')
    rows, off = cui.build_rows(close, dt.datetime(2026, 11, 25, 17, 0, tzinfo=uc.NY))
    assert [r['base_date'] for r in rows] == ['2026-11-24', '2026-11-25'] and off == []
    assert rows[-1]['spx'] is None


def test_build_rows_drops_unfinished_today():
    pd = pytest.importorskip('pandas')
    import collect_us_index as cui

    syms = list(cui.SYMS.values())
    close = pd.DataFrame({s: [1.0, 1.0] for s in syms}, index=pd.to_datetime(['2026-11-24', '2026-11-25']))
    rows, _ = cui.build_rows(close, dt.datetime(2026, 11, 25, 15, 0, tzinfo=uc.NY))
    assert [r['base_date'] for r in rows] == ['2026-11-24']


# ── job_infra._job(us_holiday=True) · get_missing_jobs ───────────────

@pytest.fixture
def ji(monkeypatch):
    """job_infra를 Supabase · 텔레그램 · KIS 없이 불러온다(스텁은 이 테스트 동안만)."""
    cfg = types.ModuleType('config')
    cfg.DEFAULT_CHAT_ID, cfg.INDUSTRY_CHAT_IDS, cfg.COMPANY_CHAT_IDS, cfg.COMPANY_CODES = '0', {}, {}, {}
    mgr = types.ModuleType('managers')
    mgr.market_timer = types.SimpleNamespace(is_kr_holiday=lambda: False)
    sa = types.ModuleType('stock_api')
    sa.send_telegram = lambda *a, **k: True
    for name, mod in (('config', cfg), ('managers', mgr), ('stock_api', sa),
                      ('supabase_bridge', types.ModuleType('supabase_bridge'))):   # bridge 없음 → _BRIDGE_OK=False
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, 'job_infra', raising=False)
    import job_infra
    import job_state
    job_state._reset()
    yield job_infra
    job_state._reset()
    sys.modules.pop('job_infra', None)


def test_guard_skips_body_on_us_holiday(ji, monkeypatch):
    import job_state
    calls = []

    @ji._job(us_holiday=True)
    def job_fake_us():
        calls.append(1)

    @ji._job()
    def job_fake_plain():
        calls.append(2)

    monkeypatch.setattr(ji.us_calendar, 'holiday_session', lambda now=None: D(2026, 11, 26))
    job_fake_us()
    job_fake_plain()
    assert calls == [2]                                   # 가드 없는 잡은 그대로 돈다
    assert 'job_fake_us' in job_state._DONE_START          # 건너뜀도 후행 잡에겐 '끝난 것'
    assert 'job_fake_us' not in ji._JOB_RESULTS            # 실행 기록(성공 · 실패)은 남기지 않는다

    monkeypatch.setattr(ji.us_calendar, 'holiday_session', lambda now=None: None)
    job_fake_us()
    assert calls == [2, 1] and ji._JOB_RESULTS['job_fake_us']['ok'] is True


def test_missing_jobs_excludes_us_holiday_guard(ji, monkeypatch):
    @ji._job(us_holiday=True)
    def job_fake_us():
        pass

    @ji._job()
    def job_fake_plain():
        pass

    monkeypatch.setattr(ji, '_EXPECTED_JOBS', {'job_fake_us': {'at': '00:00', 'day': 'every'},
                                               'job_fake_plain': {'at': '00:00', 'day': 'every'}})
    monkeypatch.setattr(ji.us_calendar, 'holiday_session', lambda now=None: D(2026, 11, 26))
    missing, _ = ji.get_missing_jobs(set())
    assert [n for n, _ in missing] == ['job_fake_plain']   # 휴장일에 건너뛴 미국 잡은 '미실행'이 아니다
    monkeypatch.setattr(ji.us_calendar, 'holiday_session', lambda now=None: None)
    missing, _ = ji.get_missing_jobs(set())
    assert sorted(n for n, _ in missing) == ['job_fake_plain', 'job_fake_us']
