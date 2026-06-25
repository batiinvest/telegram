"""
db_client.py — 수집 스크립트용 Supabase 클라이언트 팩토리
────────────────────────────────────────────────────────
collect_*.py / backfill_*.py 등 17개 파일에서 각자
create_client(SB_URL, SB_SERVICE_KEY)를 호출하는 중복을 통합합니다.

사용법:
    from db_client import get_supabase_client
    sb = get_supabase_client()

봇 런타임용 클라이언트: supabase_bridge.bridge._get_client() 사용
수집 스크립트용 클라이언트: 이 파일 사용 (bridge에 의존하지 않음)
"""

import os
import logging

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

# 과거 스크립트마다 서로 다른 환경변수명을 써온 이력이 있어
# 모든 변형을 우선순위대로 흡수한다(superset). 이렇게 하면 어떤 .env
# 구성이든 개별 파일이 기존에 인식하던 변수명을 잃지 않는다.
_URL_KEYS = ("SB_URL", "SUPABASE_URL")
_KEY_KEYS = ("SB_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY", "SB_KEY")
_client = None


def _resolve(names):
    """주어진 환경변수명들을 순서대로 시도해 첫 비어있지 않은 값 반환."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return ""


def get_supabase_client():
    """
    서비스 키 Supabase 클라이언트 싱글톤 반환.
    URL: SB_URL / SUPABASE_URL 중 하나,
    KEY: SB_SERVICE_KEY / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY / SB_KEY 중 하나.
    (호출 시점에 환경변수를 해석하므로 import 순서/지연 load_dotenv 영향 없음)
    """
    global _client
    if _client:
        return _client
    url = _resolve(_URL_KEYS)
    key = _resolve(_KEY_KEYS)
    if not url or not key:
        raise RuntimeError(
            "Supabase 환경변수 미설정: "
            "URL(SB_URL/SUPABASE_URL) 및 "
            "KEY(SB_SERVICE_KEY/SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY/SB_KEY) "
            "중 각각 하나 이상 설정 필요"
        )
    try:
        from supabase import create_client
        _client = create_client(url, key)
        log.info("[db_client] Supabase 연결 완료")
    except ImportError:
        raise RuntimeError("supabase 패키지 없음: pip install supabase")
    return _client
