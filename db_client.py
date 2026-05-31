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

_SB_URL = os.getenv("SB_URL", "")
_SB_KEY = os.getenv("SB_SERVICE_KEY", "")
_client = None


def get_supabase_client():
    """
    서비스 키 Supabase 클라이언트 싱글톤 반환.
    환경변수 SB_URL / SB_SERVICE_KEY 필요.
    """
    global _client
    if _client:
        return _client
    if not _SB_URL or not _SB_KEY:
        raise RuntimeError(
            "Supabase 환경변수 미설정: SB_URL, SB_SERVICE_KEY 확인"
        )
    try:
        from supabase import create_client
        _client = create_client(_SB_URL, _SB_KEY)
        log.info("[db_client] Supabase 연결 완료")
    except ImportError:
        raise RuntimeError("supabase 패키지 없음: pip install supabase")
    return _client
