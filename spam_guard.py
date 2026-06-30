# -*- coding: utf-8 -*-
"""광고/홍보 메시지 모더레이션.

정책: 고신뢰 광고 의심 메시지는 **자동 삭제**만 하고, **차단은 관리자 버튼 확인 후**.
탐지(종목방 오탐 최소화 — 키워드 의존 회피):
  ① 텔레그램 외부 초대링크 (t.me/+, joinchat)
  ② 카카오 오픈채팅 링크
  ③ 다중방 도배(같은 문구가 여러 방에 짧은 시간 내)
제외: 봇, 각 방 관리자(캐시), 화이트리스트(관리자 본인).
실제 차단은 봇 banChatMember (관리자 콜백 SPAM|ban/banall).
"""
import os
import re
import time
import html as _html
import hashlib
import logging

import requests
from dotenv import load_dotenv

load_dotenv("/home/kjhofone/.env")

try:
    from config import TELEGRAM_BOT_TOKEN as _BOT_TOKEN
except Exception:
    _BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

try:
    from telegram_utils import get_admin_chat_id as _get_admin_chat
except Exception:
    def _get_admin_chat(fallback=""):
        return os.environ.get("ADMIN_CHAT_ID", fallback)

log = logging.getLogger(__name__)

# 화이트리스트 (절대 건드리지 않음) — 관리자 본인
_EXEMPT_IDS = {533725430}

# ── 탐지 패턴 ──────────────────────────────────────────────
_INVITE_RE = re.compile(r"(t\.me/\+|t\.me/joinchat|telegram\.me/joinchat|tg://join)", re.IGNORECASE)
_KAKAO_RE = re.compile(r"(open\.kakao\.com|openchat|오픈\s*채팅|오픈\s*카톡)", re.IGNORECASE)

# 도배 추적: text_hash -> [(chat_id, ts)]
_recent = {}
_FLOOD_WINDOW = 600     # 10분
_FLOOD_ROOMS = 3        # 3개 방 이상
_FLOOD_MINLEN = 12

# 방별 관리자 캐시: chat_id -> (set(ids), ts)
_admin_cache = {}
_ADMIN_TTL = 3600


def _tg(method, **params):
    if not _BOT_TOKEN:
        return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{_BOT_TOKEN}/{method}",
                          json=params, timeout=10)
        return r.json()
    except Exception as e:
        log.debug(f"[spam][tg] {method} 오류: {e}")
        return {}


def _esc(s):
    return _html.escape(str(s or ""), quote=False)


def _is_flood(text, chat_id):
    t = text.strip()
    if len(t) < _FLOOD_MINLEN:
        return False
    h = hashlib.md5(t.encode("utf-8", "ignore")).hexdigest()
    now = time.time()
    lst = [x for x in _recent.get(h, []) if now - x[1] < _FLOOD_WINDOW]
    rooms = {c for c, _ in lst}
    rooms.add(chat_id)
    lst.append((chat_id, now))
    _recent[h] = lst
    if len(_recent) > 2000:          # 메모리 가드
        for k in list(_recent)[:1000]:
            _recent.pop(k, None)
    return len(rooms) >= _FLOOD_ROOMS


def _detect(text, chat_id):
    if _INVITE_RE.search(text):
        return True, "텔레그램 외부 초대링크"
    if _KAKAO_RE.search(text):
        return True, "카카오 오픈채팅"
    if _is_flood(text, chat_id):
        return True, "다중방 도배"
    return False, ""


def _fetch_admins(chat_id):
    res = _tg("getChatAdministrators", chat_id=chat_id)
    if not res.get("ok"):
        return set()
    return {a["user"]["id"] for a in res.get("result", [])}


def _is_chat_admin(chat_id, uid):
    ent = _admin_cache.get(chat_id)
    now = time.time()
    if not ent or now - ent[1] > _ADMIN_TTL:
        ids = _fetch_admins(chat_id)
        _admin_cache[chat_id] = (ids, now)
        ent = (ids, now)
    return uid in ent[0]


def check_message(scanner, message):
    """그룹 메시지 1건 검사 → 광고 의심 시 삭제 + 관리자 알림. (차단은 콜백)"""
    chat = message.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return
    text = message.get("text") or message.get("caption") or ""
    if not text:
        return
    frm = message.get("from", {})
    uid = frm.get("id")
    if not uid or frm.get("is_bot") or uid in _EXEMPT_IDS:
        return
    chat_id = chat.get("id")
    is_spam, reason = _detect(text, chat_id)
    if not is_spam:
        return
    if _is_chat_admin(chat_id, uid):
        return  # 관리자 발언은 제외
    # 자동 삭제
    try:
        scanner.delete_message(chat_id, message.get("message_id"))
    except Exception as e:
        log.debug(f"[spam] 삭제 오류: {e}")
    _notify_admin(chat, frm, text, reason)
    log.info(f"[spam] 삭제 chat={chat_id} uid={uid} reason={reason}")


def _notify_admin(chat, frm, text, reason):
    admin = _get_admin_chat()
    if not admin:
        return
    uid = frm.get("id")
    name = frm.get("first_name") or ""
    uname = ("@" + frm["username"]) if frm.get("username") else ""
    cid = chat.get("id")
    title = chat.get("title") or str(cid)
    body = (
        "🚫 <b>광고 의심 — 자동 삭제됨</b>\n\n"
        f"방: <b>{_esc(title)}</b>\n"
        f"발신: {_esc(name)} {uname} (id <code>{uid}</code>)\n"
        f"사유: {reason}\n"
        f"내용: {_esc(text[:300])}\n\n"
        f"발신자를 차단할까요?"
    )
    kb = {"inline_keyboard": [[
        {"text": "🚫 이 방 차단", "callback_data": f"SPAM|ban|{uid}|{cid}"},
        {"text": "🚫 전체 차단", "callback_data": f"SPAM|banall|{uid}"},
        {"text": "✅ 정상", "callback_data": "SPAM|ok"},
    ]]}
    _tg("sendMessage", chat_id=admin, parse_mode="HTML", text=body, reply_markup=kb)


# ── 차단 (관리자 콜백에서 호출) ────────────────────────────
def ban_in(chat_id, uid):
    return _tg("banChatMember", chat_id=int(chat_id), user_id=int(uid)).get("ok", False)


def ban_all(uid):
    """모든 숫자 chat_id 방에서 차단. 차단 성공 방 수 반환."""
    try:
        from db_client import get_client
        sb = get_client()
    except Exception:
        from supabase import create_client
        sb = create_client(os.environ["SB_URL"], os.environ["SB_SERVICE_KEY"])
    rooms = [r for r in (sb.table("rooms").select("chat_id").execute().data or [])
             if str(r.get("chat_id") or "").lstrip("-").isdigit()]
    n = 0
    for r in rooms:
        if _tg("banChatMember", chat_id=int(r["chat_id"]), user_id=int(uid)).get("ok"):
            n += 1
    return n
