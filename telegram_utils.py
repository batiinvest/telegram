"""
telegram_utils.py — 텔레그램 공통 유틸
────────────────────────────────────────
여러 파일에 중복 정의된 공통 로직을 단일 진입점으로 통합합니다.

포함:
  - get_admin_chat_id()  : bot_commands / pro_channel / sms_webhook / run_all 4중 중복 통합
  - LOW_TRUST_SOURCES    : news_main / realtime_alert 2중 중복 통합
"""

import logging

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ── 저신뢰 뉴스 출처 (기본값 — DB에서 덮어씀) ────────────────────────────────
LOW_TRUST_SOURCES: list[str] = [
    'blog.naver', 'cafe.naver', 'tistory', 'brunch',
    'newspim', 'fntoday', 'edaily',
]


# ── 어드민 채팅방 ID ──────────────────────────────────────────────────────────

def get_admin_chat_id(fallback: str = '') -> str:
    """
    app_config에서 admin_chat_id 단일 진입점.
    우선순위: pro_admin_chat_id → admin_chat_id → fallback

    bot_commands.py / pro_channel.py / sms_webhook.py / run_all.py 의
    _get_admin_chat() 4중 중복을 통합합니다.
    supabase_bridge.bridge.get_admin_chat_id() 에 위임.
    """
    try:
        from supabase_bridge import bridge as _bridge
        return _bridge.get_admin_chat_id(fallback=fallback)
    except Exception as e:
        log.debug(f"[telegram_utils] get_admin_chat_id 실패: {e}")

    # bridge 실패 시 config 폴백
    if not fallback:
        try:
            from config import DEFAULT_CHAT_ID
            return DEFAULT_CHAT_ID or ''
        except Exception:
            pass
    return fallback
