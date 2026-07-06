"""
kis_client.py — KIS 시세 조회 클라이언트
─────────────────────────────────────────
stock_api.py 물리 분할 (2026-07): KIS API 호출 위임과 inquire-price 메모리 캐시.
수집기(collect_*)는 메시지 계층(stock_api) 대신 이 모듈만 import 한다
(수집기 → 메시지 계층 역방향 의존 해소).

호출량 제한(RateLimiter)·토큰 관리는 managers.kis_auth 가 담당.
"""
import time
import threading
from typing import Optional, Dict, Any

from managers import kis_auth as _auth_manager


def _call_kis_api(tr_id: str, path: str, code: str, extra_params: Dict = None,
                  custtype: str = None, timeout: int = 10) -> Optional[Dict]:
    """매니저의 call_api로 위임"""
    return _auth_manager.call_api(tr_id, path, code, extra_params, custtype, timeout)


# ── 시세 메모리 캐시 ──────────────────────────────────────────
# 스캐너(30초 주기)·뉴스봇·공시봇이 같은 종목 inquire-price를 각자 호출 → KIS 한도 낭비.
# 스캐너가 채운 최신값을 TTL 내 재사용한다. 정밀 시세가 필요하면 max_age=0으로 우회.
_PRICE_CACHE: Dict[str, tuple] = {}      # clean_code → (output, monotonic_ts)
_PRICE_CACHE_TTL = 45.0
_price_cache_lock = threading.Lock()


def get_raw_price(code: str, max_age: float = _PRICE_CACHE_TTL) -> Optional[Dict[str, Any]]:
    clean = str(code).split('.')[0]
    now = time.monotonic()
    if max_age:
        with _price_cache_lock:
            hit = _PRICE_CACHE.get(clean)
            if hit and now - hit[1] < max_age:
                return hit[0]
    data = _call_kis_api(tr_id="FHKST01010100", path="quotations/inquire-price", code=clean)
    out = data['output'] if data else None
    if out:
        with _price_cache_lock:
            if len(_PRICE_CACHE) > 4000:   # 전체 상장사 수집 시 무한 증식 방지
                _PRICE_CACHE.clear()
            _PRICE_CACHE[clean] = (out, now)
    return out
