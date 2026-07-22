"""
job_infra.py — 스케줄 잡 공통 인프라
────────────────────────────────────
run_all.py 물리 분할 (2026-07): 잡 데코레이터(_job)·실행 결과 기록·실패 알림·
브로드캐스트 헬퍼·Supabase 브릿지 접근을 jobs_collect / jobs_briefing /
watchdog_flags 가 공유한다.
"""
import time
import logging
import datetime
import threading
import functools

import stock_api
from managers import market_timer
from telegram_utils import get_admin_chat_id as _get_admin_chat_id
from config import DEFAULT_CHAT_ID, INDUSTRY_CHAT_IDS, COMPANY_CHAT_IDS, COMPANY_CODES

# ✅ Supabase 브릿지 (실패해도 스케줄러 동작에 영향 없음)
try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
    logging.info("✅ [Bridge] Supabase 브릿지 연결 완료")
    # 봇 시작 시 현재 모니터링 목록 스냅샷 (reload 비교용)
    try:
        _sb_init = _bridge._get_client()
        _init_mon = _sb_init.table('companies').select('code').eq('is_monitored', True).execute()
        _bridge._prev_mon_codes = {r['code'].split('.')[0] for r in (_init_mon.data or [])}
        logging.info(f"📋 [Bridge] 모니터링 종목 {len(_bridge._prev_mon_codes)}개 초기화")
    except Exception as _ie:
        _bridge._prev_mon_codes = set()
except Exception as _be:
    _bridge = None
    _BRIDGE_OK = False
    logging.warning(f"⚠️ [Bridge] Supabase 브릿지 로드 실패 (스케줄 DB 제어 비활성화): {_be}")


def _start_daemon(target, name: str, args: tuple = ()):
    """daemon=True 스레드 생성 후 즉시 시작. threading.Thread 4줄 스폰 패턴 통일."""
    threading.Thread(target=target, name=name, args=args, daemon=True).start()


def _broadcast_to_industries(
    api_func,
    *args,
    sleep: float = 0.5,
    keyboard=None,
    skip: tuple = ("바티인베스트",),
    label: str = "산업",
):
    """
    INDUSTRY_CHAT_IDS 전체를 순회해서 api_func(ind_name, *args) 결과를 발송.

    Args:
        api_func : stock_api의 산업별 메시지 생성 함수
        *args    : api_func에 industry_name 뒤로 전달할 추가 인자
        sleep    : 채널 간 대기 시간 (초)
        keyboard : 텔레그램 키보드 버튼
        skip     : 제외할 채널명 튜플
        label    : 로그용 레이블

    Example:
        _broadcast_to_industries(stock_api.get_industry_theme_ranking,
                                  keyboard=COMMON_BUTTON, label="점심 브리핑")
    """
    for ind_name, chat_id in INDUSTRY_CHAT_IDS.items():
        if ind_name in skip:
            continue
        try:
            msg = api_func(ind_name, *args)
            if msg:
                stock_api.send_telegram(chat_id, msg, keyboard=keyboard)
            time.sleep(sleep)
        except Exception as e:
            logging.error(f"❌ [{label}] 산업 발송 오류 ({ind_name}): {e}")


def _broadcast_to_companies(
    api_func,
    *args,
    sleep: float = 0.5,
    keyboard=None,
    label: str = "종목",
):
    """
    COMPANY_CHAT_IDS 전체를 순회해서 api_func(stock_code, comp_name, *args) 결과를 발송.
    COMPANY_CODES에 없는 종목(코드 미등록)은 자동 스킵.

    Args:
        api_func  : stock_api의 종목별 메시지 생성 함수
                    시그니처: f(code, name, *args) → str | None
        sleep     : 채널 간 대기 시간 (초)
        keyboard  : 텔레그램 키보드 버튼
        label     : 로그용 레이블

    Example:
        _broadcast_to_companies(stock_api.get_stock_detail,
                                 keyboard=COMMON_BUTTON, label="마감 브리핑")
    """
    for comp_name, chat_id in COMPANY_CHAT_IDS.items():
        code = COMPANY_CODES.get(comp_name)
        if not code:
            continue
        try:
            msg = api_func(code, comp_name, *args)
            if msg:
                stock_api.send_telegram(chat_id, msg, keyboard=keyboard)
            time.sleep(sleep)
        except Exception as e:
            logging.error(f"❌ [{label}] 종목 발송 오류 ({comp_name}): {e}")


def _is_enabled(job_key: str) -> bool:
    """DB에서 스케줄 활성화 여부 확인. 브릿지 없으면 항상 True."""
    if not _BRIDGE_OK:
        return True
    try:
        return _bridge.is_schedule_enabled(job_key)
    except Exception:
        return True


# ── 잡 실행 결과 기록 (일일 운영 요약·실패 알림용, 인메모리) ──────────────────
_JOB_RESULTS: dict = {}    # fn 이름 → {'date','time','ok','error','elapsed'}
_FAIL_ALERTED: set = set() # (date, fn 이름) — 동일 잡 실패 알림 하루 1회 제한

# 잡 본문이 예외를 자체 처리(로그만)하고 정상 반환해도 실패로 기록하기 위한 마커.
# 스레드 로컬 — _threaded 잡이 병렬 실행돼도 서로 섞이지 않는다.
_JOB_LOCAL = threading.local()

_JOB_GUARDS: dict = {}     # fn 이름 → {'key','holiday','weekday_only'} (데코레이션 시점 등록)
_EXPECTED_JOBS: dict = {}  # fn 이름 → {'at':'HH:MM','day':'every'|'saturday'|...}


def mark_failed(reason):
    """잡 본문이 예외를 삼키더라도 이 잡을 '실패'로 기록시킨다. 흐름은 그대로 진행.

    구: 대부분의 잡이 `except Exception as e: logging.error(...)`로 삼켜서
        수집이 통째로 실패해도 job_runs에 ok=true로 남고 운영요약이 "전 잡 정상"을 찍었다.
    """
    fails = getattr(_JOB_LOCAL, 'failures', None)
    if fails is not None:
        fails.append(str(reason)[:200])


def set_expected_jobs(jobs):
    """run_all이 스케줄 등록 직후 호출 — schedule.Job 목록에서 잡 이름·예정시각·요일 추출.
    같은 잡이 여러 번 등록되면 가장 이른 시각을 기준으로 삼는다(그 시각이 지났으면 실행됐어야 함)."""
    _EXPECTED_JOBS.clear()
    for j in jobs:
        jf = getattr(j, 'job_func', None)
        name = getattr(jf, '__name__', None) or getattr(getattr(jf, 'func', None), '__name__', None)
        if not name:
            continue
        at = j.at_time.strftime('%H:%M') if getattr(j, 'at_time', None) else ''
        day = getattr(j, 'start_day', None) or 'every'
        cur = _EXPECTED_JOBS.get(name)
        if cur is None or (at and cur['at'] and at < cur['at']):
            _EXPECTED_JOBS[name] = {'at': at, 'day': day}
    logging.info(f"🗓 [스케줄] 기대 잡 {len(_EXPECTED_JOBS)}개 등록 (미실행 탐지용)")


def get_missing_jobs(ran) -> tuple:
    """(미실행, 비활성) — 오늘 예정시각이 지났는데 실행 기록이 없는 잡.
    가드(주말/휴장일/DB토글)는 데코레이터 등록값으로 판정 — 재시작에 영향받지 않는다."""
    if not _EXPECTED_JOBS:
        return [], []
    now = datetime.datetime.now()
    wd = now.strftime('%A').lower()
    now_hhmm = now.strftime('%H:%M')
    is_weekend = now.weekday() >= 5
    try:
        holiday = market_timer.is_kr_holiday()
    except Exception:
        holiday = False
    missing, disabled = [], []
    for name, meta in sorted(_EXPECTED_JOBS.items()):
        if name in ran:
            continue
        if meta['day'] != 'every' and meta['day'] != wd:
            continue
        if meta['at'] and meta['at'] > now_hhmm:
            continue
        g = _JOB_GUARDS.get(name, {})
        if g.get('weekday_only') and is_weekend:
            continue
        if g.get('holiday') and holiday:
            continue
        k = g.get('key')
        if k and not _is_enabled(k):
            disabled.append(name)
            continue
        missing.append((name, meta['at']))
    return missing, disabled


def _record_job(fn_name: str, ok: bool, error=None, elapsed: float = 0.0):
    _JOB_RESULTS[fn_name] = {
        'date':    datetime.date.today().isoformat(),
        'time':    datetime.datetime.now().strftime('%H:%M'),
        'ok':      ok,
        'error':   str(error)[:200] if error else None,
        'elapsed': round(elapsed, 1),
    }
    _record_job_db(fn_name, ok, error, elapsed)


_JOB_RUNS_DB_OK = True   # job_runs 미생성/장애 시 False — 매 잡마다 실패 로그 스팸 방지


def _record_job_db(fn_name: str, ok: bool, error, elapsed: float):
    """잡 결과를 job_runs 테이블에 영속 기록 (best-effort).
    인메모리 _JOB_RESULTS는 재시작에 날아가므로 운영요약·freshness·재처리는 이쪽 기준."""
    global _JOB_RUNS_DB_OK
    if not (_BRIDGE_OK and _JOB_RUNS_DB_OK):
        return
    try:
        now = datetime.datetime.now().astimezone()
        _bridge._get_client().table('job_runs').insert({
            'run_date':    now.date().isoformat(),
            'job_name':    fn_name,
            'started_at':  (now - datetime.timedelta(seconds=elapsed)).isoformat(),
            'finished_at': now.isoformat(),
            'ok':          ok,
            'error':       str(error)[:500] if error else None,
            'elapsed_sec': round(elapsed, 1),
        }).execute()
    except Exception as e:
        if 'PGRST205' in str(e):
            _JOB_RUNS_DB_OK = False
            logging.warning("⚠️ [job_runs] 테이블 미생성 — sql/job_runs.sql 실행 전까지 기록 생략")
        else:
            logging.debug(f"[job_runs] 기록 실패: {e}")


def _notify_job_failure(fn_name: str, error):
    """잡 실패를 관리자 방으로 즉시 알림 (잡·일자당 1회)."""
    key = (datetime.date.today().isoformat(), fn_name)
    if key in _FAIL_ALERTED:
        return
    _FAIL_ALERTED.add(key)
    try:
        target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)
        stock_api.send_telegram(target, f"❌ <b>[잡 실패] {fn_name}</b>\n{str(error)[:300]}")
    except Exception:
        pass


def _job(key: str = None, *, holiday: bool = False, weekday_only: bool = False):
    """Job 함수 데코레이터: 휴장일/평일 체크 + DB 활성화 체크 + 결과 기록/실패 알림.

    Args:
        key:          DB 활성화 체크 키 (None이면 체크 안 함)
        holiday:      True면 한국 휴장일(공휴일+주말) 스킵
        weekday_only: True면 주말 스킵 (holiday보다 느슨한 조건)
    """
    def decorator(fn):
        _JOB_GUARDS[fn.__name__] = {
            'key': key, 'holiday': holiday, 'weekday_only': weekday_only,
        }

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if weekday_only and datetime.datetime.now().weekday() >= 5:
                return
            if holiday and market_timer.is_kr_holiday():
                return
            if key is not None and not _is_enabled(key):
                logging.info(f"⏸ [{key}] 비활성화 (DB 설정)")
                return
            start = time.time()
            # 중첩 잡(job_collect_market_closing → job_watchlist_alert)이 실재하므로
            # 바깥 잡의 실패 목록을 보존했다가 finally에서 되돌린다.
            prev = getattr(_JOB_LOCAL, 'failures', None)
            _JOB_LOCAL.failures = []
            try:
                result = fn(*args, **kwargs)
                fails = _JOB_LOCAL.failures
                if fails:
                    err = ' | '.join(fails)[:400]
                    logging.error(f"❌ [{fn.__name__}] 실패 감지 (본문 자체처리): {err}")
                    _record_job(fn.__name__, False, err, time.time() - start)
                    _notify_job_failure(fn.__name__, err)
                else:
                    _record_job(fn.__name__, True, elapsed=time.time() - start)
                return result
            except Exception as e:
                logging.error(f"❌ [{fn.__name__}] 잡 실행 실패: {e}", exc_info=True)
                _record_job(fn.__name__, False, e, time.time() - start)
                _notify_job_failure(fn.__name__, e)
            finally:
                _JOB_LOCAL.failures = prev
        return wrapper
    return decorator


def _log_notice(target: str, content: str):
    """발송 기록을 Supabase에 저장. 실패해도 무시."""
    if not _BRIDGE_OK:
        return
    try:
        _bridge.log_notice(target=target, content=content, sent_count=1, ok_count=1)
    except Exception:
        pass
