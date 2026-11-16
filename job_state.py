"""
job_state.py — 스케줄 잡 진행 상태(인메모리) + '선행 잡 완료 대기'
──────────────────────────────────────────────────────────────────
표준 라이브러리만 쓰는 순수 모듈(tests/test_job_state.py). job_infra·run_all이 쓴다.

왜 필요한가 (2026-11-13 저녁 실측):
  · 18:30 재무수집(31분)·18:40 추정실적(9분)이 스레드 없이 스케줄 루프를 붙잡아 18:42~19:10 잡이
    19:09:43에 한꺼번에 시작 → 어닝 서프라이즈 브리핑(19:10:09)이 재조정(19:10:13 완료)보다 먼저
    나가 그날 새로 잡힌 17건이 빠졌다. 순서를 '예약 시각 간격'에만 맡긴 탓이다.
  · 19:25 재처리 잡은 아직 돌고 있던 job_flow_empty를 '실행 기록 없음 = 미실행'으로 보고 한 번 더
    겹쳐 돌렸다(루프가 19:56까지 다시 막힘, 11월에만 4번).

그래서 ① 순서가 중요한 잡은 예약 시각이 아니라 선행 잡의 '완료'에 묶고(wait_for),
       ② 지금 돌거나 기다리는 잡은 '미실행'과 구분한다(active / is_active).
재시작하면 비워진다 — 그날 예정 시각을 놓친 선행 잡은 '시작한 흔적 없음'으로 판정해 기다리지 않는다.
"""
import time
import logging
import datetime
import threading

_LOCK = threading.Lock()
_IN_FLIGHT: dict = {}    # 잡 이름 → 띄워져 아직 안 끝난 실행 수 (선행 잡 대기·가드 확인·본문 포함)
_WAITING: dict = {}      # 잡 이름 → 선행 잡 완료를 기다리는 실행 수 (표시용)
_DONE_START: dict = {}   # 잡 이름 → 끝난 실행(가드 스킵 포함) 가운데 가장 늦은 '시작' 시각


def enter(name: str) -> datetime.datetime:
    """실행 진입 표시. 돌려받은 시작 시각을 leave()에 넘긴다.
    run_all._threaded는 스레드를 띄우기 **전에**(스케줄 루프 안에서) 부른다 — 뒤이어 뜨는 후행 잡이
    '선행 잡이 아직 시작도 안 했다'고 오판할 틈이 없도록."""
    with _LOCK:
        _IN_FLIGHT[name] = _IN_FLIGHT.get(name, 0) + 1
    return datetime.datetime.now()


def leave(name: str, began: datetime.datetime = None):
    """실행 종료 표시(성공·실패 무관). began을 주면 '그 시각에 시작한 실행이 끝났다'고 기록한다."""
    with _LOCK:
        n = _IN_FLIGHT.get(name, 0) - 1
        if n > 0:
            _IN_FLIGHT[name] = n
        else:
            _IN_FLIGHT.pop(name, None)
        if began is not None:
            cur = _DONE_START.get(name)
            if cur is None or began > cur:
                _DONE_START[name] = began


def skipped(name: str):
    """가드(주말·휴장일·DB 토글)로 본문을 건너뜀 — 후행 잡 입장에선 '끝난 것'과 같다."""
    now = datetime.datetime.now()
    with _LOCK:
        cur = _DONE_START.get(name)
        if cur is None or now > cur:
            _DONE_START[name] = now


def is_active(name: str) -> bool:
    with _LOCK:
        return _IN_FLIGHT.get(name, 0) > 0


def active() -> tuple:
    """(도는 중, 선행 잡 대기 중) 잡 이름 목록 — 운영요약·재처리가 '미실행'과 구분하는 데 쓴다."""
    with _LOCK:
        flying = set(_IN_FLIGHT)
        waiting = set(_WAITING)
    return sorted(flying - waiting), sorted(waiting)


def wait_for(waiter: str, deps, timeout_sec: float, grace_sec: float = 60.0,
             poll: float = 2.0, on_timeout=None) -> bool:
    """선행 잡이 끝날 때까지 기다린다.

    deps: [(선행 잡 이름, 'HH:MM'), ...] — 오늘 그 시각 이후에 시작한 선행 잡 실행이 끝났고,
          지금 돌고 있는 실행도 없어야 통과한다. 가드로 건너뛴 것도 끝난 것으로 친다.
          선행 잡의 성공·실패는 가리지 않는다(예전 직렬 실행과 같은 조건).
    기다리지 못하는 경우 — 어느 쪽이든 False를 돌려주고, 호출부는 본 잡을 그대로 실행한다:
      · 선행 잡이 시작한 흔적이 없다(재시작으로 예정 시각을 놓친 날 등) → grace_sec 뒤 포기
      · timeout_sec를 넘겼다 → on_timeout(선행 잡 이름, 기다린 초) 호출 뒤 포기
    예정 시각 전에 불린 선행 조건(수동 실행 등)은 묶지 않는다."""
    now = datetime.datetime.now()
    targets = []
    for name, at in deps:
        hh, mm = str(at).split(':')[:2]
        since = datetime.datetime.combine(now.date(), datetime.time(int(hh), int(mm)))
        if since <= now:
            targets.append((name, at, since))
    if not targets:
        return True

    t0 = time.time()
    announced = False
    ok = True
    with _LOCK:
        _WAITING[waiter] = _WAITING.get(waiter, 0) + 1
    try:
        for name, at, since in targets:
            while True:
                with _LOCK:
                    flying = _IN_FLIGHT.get(name, 0) > 0
                    ds = _DONE_START.get(name)
                done = ds is not None and ds >= since
                if done and not flying:
                    break
                waited = time.time() - t0
                if not flying and not done and waited >= grace_sec:
                    logging.warning(f"⚠️ [순서] {waiter} — 선행 {name}({at})이 시작한 흔적이 없어 기다리지 않고 실행")
                    ok = False
                    break
                if waited >= timeout_sec:
                    logging.warning(f"⚠️ [순서] {waiter} — 선행 {name}({at})이 {waited / 60:.0f}분째 안 끝나 먼저 실행")
                    if on_timeout:
                        try:
                            on_timeout(name, waited)
                        except Exception:
                            pass
                    return False
                if not announced:
                    logging.info(f"⏳ [순서] {waiter} — 선행 {name}({at}) 완료 대기")
                    announced = True
                time.sleep(poll)
        if announced:
            logging.info(f"▶️ [순서] {waiter} — {time.time() - t0:.0f}초 기다린 뒤 시작")
        return ok
    finally:
        with _LOCK:
            n = _WAITING.get(waiter, 0) - 1
            if n > 0:
                _WAITING[waiter] = n
            else:
                _WAITING.pop(waiter, None)


def _reset():
    """테스트용 — 상태 비우기."""
    with _LOCK:
        _IN_FLIGHT.clear()
        _WAITING.clear()
        _DONE_START.clear()
