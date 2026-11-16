"""supabase_bridge.check_disclosure_flag — 봇이 뜰 때 예전 플래그로 재무수집을 다시 돌리지 않는다.

2026-11-11~11-17 기동 9번 모두 첫 워치독 확인(기동 약 70초 뒤)에서 몇 달 전 값을 '방금 누름'으로 읽어
job_collect_financials 전체를 한 번 더 돌렸다(마감일 11-16엔 1,400곳·약 1시간, 18:30 정규 실행과 겹침).
가짜 클라이언트만 쓴다 — .env·DB·네트워크 불요."""
import time

import supabase_bridge as sbm


class _Res:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    """app_config의 run_disclosure_flag 한 행만 흉내 — value=None이면 행 없음, boom=True면 조회 실패."""
    def __init__(self, value=None):
        self.value = value
        self.boom = False

    def table(self, name):
        assert name == 'app_config'
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, key, val):
        assert (key, val) == ('key', 'run_disclosure_flag')
        return self

    def execute(self):
        if self.boom:
            raise RuntimeError('Server disconnected')
        return _Res([] if self.value is None else [{'value': self.value}])


def _bridge(client):
    b = sbm.SupabaseBridge()
    b._get_client = lambda: client
    return b


def _ms(sec_ago: float) -> str:
    return str(int((time.time() - sec_ago) * 1000))


def test_old_flag_at_startup_does_not_trigger():
    c = _FakeClient('1778068347196')            # 서버의 실제 값(2026-05)
    b = _bridge(c)
    assert b.check_disclosure_flag() is False   # 뜬 뒤 첫 확인 — 기준만 잡는다
    assert b.check_disclosure_flag() is False   # 그대로면 계속 False


def test_press_after_startup_triggers_once():
    c = _FakeClient(_ms(86400))
    b = _bridge(c)
    assert b.check_disclosure_flag() is False
    c.value = _ms(0)                             # 누름
    assert b.check_disclosure_flag() is True
    assert b.check_disclosure_flag() is False   # 같은 값은 한 번만


def test_press_during_restart_still_runs():
    c = _FakeClient(_ms(90))                     # 재시작하는 사이(90초 전)에 누름
    b = _bridge(c)
    assert b.check_disclosure_flag() is True
    assert b.check_disclosure_flag() is False


def test_non_timestamp_value_at_startup_is_baseline_only():
    c = _FakeClient('run')
    b = _bridge(c)
    assert b.check_disclosure_flag() is False
    c.value = 'run2'
    assert b.check_disclosure_flag() is True


def test_row_created_after_startup_triggers():
    c = _FakeClient(None)                        # 행 없음
    b = _bridge(c)
    assert b.check_disclosure_flag() is False
    c.value = _ms(5)
    assert b.check_disclosure_flag() is True


def test_row_removed_is_not_a_press():
    c = _FakeClient('1778068347196')
    b = _bridge(c)
    assert b.check_disclosure_flag() is False
    c.value = None
    assert b.check_disclosure_flag() is False
    c.value = '1778068347196'                    # 같은 값으로 되살아남 — 누름 아님
    assert b.check_disclosure_flag() is False


def test_failed_first_read_does_not_trigger_on_next_read():
    c = _FakeClient('1778068347196')
    c.boom = True
    b = _bridge(c)
    assert b.check_disclosure_flag() is False   # 조회 실패 — 기준을 못 잡음
    c.boom = False
    assert b.check_disclosure_flag() is False   # 다음 성공이 '첫 확인'


def test_no_client_is_false():
    b = sbm.SupabaseBridge()
    b._get_client = lambda: None
    assert b.check_disclosure_flag() is False
