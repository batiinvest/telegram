"""job_state — 잡 진행 상태·'선행 잡 완료 대기' (순수 모듈, .env·DB·네트워크 불요).

지키려는 것(2026-11-13 사고): 후행 잡은 선행 잡이 '끝난 뒤'에만 시작하고,
도는 중인 잡은 '미실행'으로 보이지 않는다."""
import time
import datetime
import threading

import pytest

import job_state as js


@pytest.fixture(autouse=True)
def _clean():
    js._reset()
    yield
    js._reset()


def _at_now() -> str:
    """지금이 속한 분 — 선행 잡의 '예약 시각' 대역."""
    return datetime.datetime.now().strftime('%H:%M')


def _wait(waiter, deps, **kw):
    kw.setdefault('timeout_sec', 5)
    kw.setdefault('grace_sec', 0.2)
    kw.setdefault('poll', 0.01)
    return js.wait_for(waiter, deps, **kw)


def test_already_finished_passes_immediately():
    at = _at_now()
    began = js.enter('a')
    js.leave('a', began)
    t0 = time.time()
    assert _wait('b', [('a', at)]) is True
    assert time.time() - t0 < 0.1


def test_waits_until_running_dep_finishes():
    at = _at_now()
    began = js.enter('a')
    done_at = {}

    def finish():
        time.sleep(0.3)
        done_at['t'] = time.time()
        js.leave('a', began)
    threading.Thread(target=finish, daemon=True).start()
    assert _wait('b', [('a', at)]) is True
    assert time.time() >= done_at['t']          # 선행 잡이 끝난 뒤에야 통과
    assert js.active() == ([], [])


def test_grace_does_not_cut_a_running_dep():
    """선행 잡이 도는 동안엔 grace(시작 흔적 없음 판정)가 지나도 계속 기다린다."""
    at = _at_now()
    began = js.enter('a')
    threading.Timer(0.4, lambda: js.leave('a', began)).start()
    t0 = time.time()
    assert _wait('b', [('a', at)], grace_sec=0.05) is True
    assert time.time() - t0 >= 0.35


def test_guard_skip_counts_as_finished():
    at = _at_now()
    js.skipped('a')
    assert _wait('b', [('a', at)]) is True


def test_dep_never_started_gives_up_after_grace():
    t0 = time.time()
    assert _wait('b', [('a', _at_now())], grace_sec=0.2) is False
    assert 0.15 <= time.time() - t0 < 1.0


def test_run_started_before_scheduled_time_does_not_count():
    """예약 시각 전에 시작한 실행(아침 회차·수동 실행)은 저녁 회차의 완료가 아니다."""
    at = _at_now()
    since = datetime.datetime.now().replace(second=0, microsecond=0)
    js._DONE_START['a'] = since - datetime.timedelta(seconds=1)
    assert _wait('b', [('a', at)], grace_sec=0.1) is False


def test_timeout_calls_back_and_returns_false():
    at = _at_now()
    began = js.enter('a')                        # 끝나지 않는 선행 잡
    seen = []
    assert _wait('b', [('a', at)], timeout_sec=0.2,
                 on_timeout=lambda dep, waited: seen.append((dep, waited))) is False
    assert seen and seen[0][0] == 'a' and seen[0][1] >= 0.2
    js.leave('a', began)


def test_dep_scheduled_later_than_now_is_not_bound():
    now = datetime.datetime.now()
    if now.hour == 23 and now.minute >= 57:
        pytest.skip('자정 직전 — 다음 분이 내일')
    later = (now + datetime.timedelta(minutes=2)).strftime('%H:%M')
    t0 = time.time()
    assert _wait('b', [('a', later)]) is True
    assert time.time() - t0 < 0.1


def test_active_separates_running_and_waiting():
    at = _at_now()
    began_a = js.enter('a')
    began_b = js.enter('b')                      # _threaded가 스레드를 띄우기 전에 표시
    seen = {}

    def b_job():
        js.wait_for('b', [('a', at)], timeout_sec=5, grace_sec=0.2, poll=0.01)
        js.leave('b', began_b)
    t = threading.Thread(target=b_job, daemon=True)
    t.start()
    time.sleep(0.1)
    seen['mid'] = js.active()
    js.leave('a', began_a)
    t.join(2)
    assert seen['mid'] == (['a'], ['b'])         # a는 도는 중, b는 선행 잡 대기
    assert js.active() == ([], [])


def test_chain_waits_through_a_waiting_middle_job():
    """재무수집 → 재조정 → 브리핑: 가운데 잡이 '대기 중'이어도 마지막 잡은 포기하지 않는다."""
    at = _at_now()
    order = []
    began_fin = js.enter('fin')
    began_rec = js.enter('rec')
    began_brf = js.enter('brf')

    def rec():
        js.wait_for('rec', [('fin', at)], timeout_sec=5, grace_sec=0.05, poll=0.01)
        b = js.enter('rec')                      # _job 래퍼의 본문 진입
        order.append('rec')
        time.sleep(0.05)
        js.leave('rec', b)
        js.leave('rec', began_rec)

    def brf():
        ok = js.wait_for('brf', [('rec', at)], timeout_sec=5, grace_sec=0.05, poll=0.01)
        order.append('brf' if ok else 'brf-early')
        js.leave('brf', began_brf)

    ts = [threading.Thread(target=f, daemon=True) for f in (rec, brf)]
    for t in ts:
        t.start()
    time.sleep(0.4)                              # grace(0.05초)를 한참 넘겨서 재무수집 종료
    order.append('fin')
    js.leave('fin', began_fin)
    for t in ts:
        t.join(3)
    assert order == ['fin', 'rec', 'brf']


def test_leave_without_began_does_not_mark_finished():
    at = _at_now()
    js.enter('a')
    js.leave('a')                                # 스레드를 못 띄운 경우
    assert js.is_active('a') is False
    assert _wait('b', [('a', at)], grace_sec=0.1) is False


def test_overlapping_runs_count_until_all_finish():
    at = _at_now()
    b1 = js.enter('a')
    b2 = js.enter('a')
    js.leave('a', b1)
    assert js.is_active('a') is True
    threading.Timer(0.2, lambda: js.leave('a', b2)).start()
    t0 = time.time()
    assert _wait('b', [('a', at)]) is True
    assert time.time() - t0 >= 0.15
