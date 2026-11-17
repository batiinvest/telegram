"""잡이 끝날 때 쓰고 남은 메모리를 OS에 돌려준다 (glibc malloc_trim).

배경(2026-11-17~18 측정, scripts/resmon.sh):
  봇은 잡이 끝나도 메모리를 돌려주지 않아 하루 새 236MB→707MB로 불어났고(서버 RAM 958MB),
  그 대부분이 스왑에 밀려 있다가 다음 잡이 그 자리를 다시 쓸 때 디스크에서 4KB씩 읽혀
  GIL을 쥔 스레드가 디스크 대기(D)에 묶이면서 봇 전체가 3~5분 멈췄다
  (11-17 19:00:46부터 186초, 20:10:07부터 322초 — CPU steal은 0).
  같은 계산(수급 빈집, 쓰기 없음)을 따로 돌려 보니 잡 뒤 남는 메모리가
  기본 할당자 175MB → 파이썬 객체 풀 끄고(PYTHONMALLOC=malloc) malloc_trim 하면 88MB.
  그래서 run_all이 PYTHONMALLOC=malloc·MALLOC_ARENA_MAX=2로 기동하고,
  job_infra._job이 잡마다 끝에서 release()를 부른다.

리눅스(glibc)가 아니면 아무것도 하지 않는다.
"""
import ctypes
import logging
import threading

_LOG_MB = 20                 # 이만큼 넘게 돌려줬을 때만 기록 — 평소 잡은 조용히
_libc = None                 # None=아직 안 찾음, False=쓸 수 없음
_lock = threading.Lock()     # 한 번의 trim이 모든 아레나를 훑는다 — 동시에 끝난 잡은 건너뛴다


def _anon_mb():
    """이 프로세스의 RSS+스왑(MB). /proc가 없으면 None."""
    try:
        rss = swap = 0
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1])
                elif line.startswith('VmSwap:'):
                    swap = int(line.split()[1])
        return (rss + swap) / 1024
    except (OSError, ValueError, IndexError):
        return None


def _load():
    global _libc
    try:
        lib = ctypes.CDLL('libc.so.6')
        lib.malloc_trim.argtypes = [ctypes.c_size_t]
        lib.malloc_trim.restype = ctypes.c_int
        _libc = lib
    except (OSError, AttributeError):
        _libc = False


def release(label: str = '') -> None:
    """빈 메모리를 OS에 돌려준다. 어떤 실패도 잡에 번지지 않는다.
    ctypes 호출은 GIL을 놓으므로 다른 스레드는 그동안 계속 돈다."""
    if _libc is None:
        _load()
    if not _libc:
        return
    if not _lock.acquire(blocking=False):
        return   # 다른 잡이 지금 돌려주는 중 — 다음 잡 끝에서 다시 훑는다
    try:
        before = _anon_mb()
        _libc.malloc_trim(0)
        after = _anon_mb()
        if before is not None and after is not None and before - after >= _LOG_MB:
            logging.info(f"🧹 [메모리] {label} 뒤 {before - after:.0f}MB 반환 ({before:.0f}→{after:.0f}MB)")
    except Exception:
        pass
    finally:
        _lock.release()
