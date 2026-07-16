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

# 스태프 화이트리스트 (강퇴 KICK_EXEMPT_IDS와 공용) — 관리자 본인 + .env
def _exempt_ids():
    ids = {533725430}
    raw = os.environ.get("KICK_EXEMPT_IDS", "")
    for x in raw.replace(" ", "").split(","):
        if x.lstrip("-").isdigit():
            ids.add(int(x))
    return ids

# ── 탐지 패턴 ──────────────────────────────────────────────
_INVITE_RE = re.compile(r"(t\.me/\+|t\.me/joinchat|telegram\.me/joinchat|tg://join)", re.IGNORECASE)
_KAKAO_RE = re.compile(r"(open\.kakao\.com|openchat|오픈\s*채팅|오픈\s*카톡)", re.IGNORECASE)

# 뉴스/정상 도메인 — 도배 판정에서 제외 (정상 뉴스 공유 오탐 방지)
_NEWS_DOMAINS = (
    "naver.com", "daum.net", "hankyung.com", "mk.co.kr", "mt.co.kr", "edaily.co.kr",
    "sedaily.com", "yna.co.kr", "yonhapnews", "chosun.com", "joongang.co.kr",
    "donga.com", "hani.co.kr", "khan.co.kr", "fnnews.com", "asiae.co.kr",
    "newspim.com", "businesspost.co.kr", "thebell.co.kr", "wowtv.co.kr",
    "mtn.co.kr", "infostockdaily", "paxnetnews", "dart.fss.or.kr", "irgo.co.kr",
    "youtube.com", "youtu.be",
)


def _has_news_link(text):
    low = text.lower()
    return any(d in low for d in _NEWS_DOMAINS)


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
    """(is_spam, reason, auto_delete). 도배는 auto_delete=False(알림만)."""
    if _INVITE_RE.search(text):
        return True, "텔레그램 외부 초대링크", True
    if _KAKAO_RE.search(text):
        return True, "카카오 오픈채팅", True
    if _is_flood(text, chat_id) and not _has_news_link(text):
        return True, "다중방 도배(의심)", False
    return False, "", False


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
    """그룹 메시지 검사 → 광고(자동삭제)·도배(알림만) → 관리자 알림."""
    chat = message.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return
    text = message.get("text") or message.get("caption") or ""
    if not text:
        return
    frm = message.get("from", {})
    uid = frm.get("id")
    if not uid or frm.get("is_bot") or uid in _exempt_ids():
        return
    chat_id = chat.get("id")
    is_spam, reason, auto_delete = _detect(text, chat_id)
    if not is_spam:
        return
    if _is_chat_admin(chat_id, uid):
        return  # 관리자 발언은 제외
    msg_id = message.get("message_id")
    if auto_delete:
        try:
            scanner.delete_message(chat_id, msg_id)
        except Exception as e:
            log.debug(f"[spam] 삭제 오류: {e}")
    _notify_admin(chat, frm, text, reason, auto_delete, msg_id)
    _act = '삭제' if auto_delete else '알림'
    log.info(f"[spam] {_act} chat={chat_id} uid={uid} reason={reason}")


def _notify_admin(chat, frm, text, reason, deleted, message_id):
    admin = _get_admin_chat()
    if not admin:
        return
    _NL = chr(10)
    uid = frm.get("id")
    name = frm.get("first_name") or ""
    uname = ("@" + frm["username"]) if frm.get("username") else ""
    cid = chat.get("id")
    title = chat.get("title") or str(cid)
    head = "🚫 <b>광고 의심 — 자동 삭제됨</b>" if deleted else "⚠️ <b>도배 의심 — 삭제 안 함</b>"
    body = (head + _NL + _NL
            + "방: <b>" + _esc(title) + "</b>" + _NL
            + "발신: " + _esc(name) + " " + uname + " (id <code>" + str(uid) + "</code>)" + _NL
            + "사유: " + reason + _NL
            + "내용: " + _esc(text[:300]))
    row = []
    if not deleted:
        row.append({"text": "🗑 삭제", "callback_data": "SPAM|del|" + str(cid) + "|" + str(message_id)})
    row.append({"text": "🚫 이 방 차단", "callback_data": "SPAM|ban|" + str(uid) + "|" + str(cid)})
    row.append({"text": "🚫 전체 차단", "callback_data": "SPAM|banall|" + str(uid)})
    row.append({"text": "✅ 정상", "callback_data": "SPAM|ok"})
    _tg("sendMessage", chat_id=admin, parse_mode="HTML", text=body, reply_markup={"inline_keyboard": [row]})


# ── 차단 (관리자 콜백에서 호출) ────────────────────────────
def ban_in(chat_id, uid):
    return _tg("banChatMember", chat_id=int(chat_id), user_id=int(uid)).get("ok", False)


def _sb_client():
    try:
        from db_client import get_client
        return get_client()
    except Exception:
        from supabase import create_client
        return create_client(os.environ["SB_URL"], os.environ["SB_SERVICE_KEY"])


def _all_room_cids():
    """모든 방 chat_id (숫자=int, @username=str). 산업방 포함 전체."""
    sb = _sb_client()
    out = []
    for r in (sb.table("rooms").select("chat_id").execute().data or []):
        c = (r.get("chat_id") or "").strip()
        if not c:
            continue
        out.append(int(c) if c.lstrip("-").isdigit() else c)
    return out


def ban_all(uid):
    """모든 방(산업 포함)에서 차단. 성공 방 수 반환."""
    n = 0
    for cid in _all_room_cids():
        if _tg("banChatMember", chat_id=cid, user_id=int(uid)).get("ok"):
            n += 1
    return n


def unban_all(uid):
    """모든 방에서 차단 해제(only_if_banned). 해제 방 수 반환."""
    n = 0
    for cid in _all_room_cids():
        if _tg("unbanChatMember", chat_id=cid, user_id=int(uid), only_if_banned=True).get("ok"):
            n += 1
    return n


def resolve_user(query):
    """id(숫자) 또는 @username -> (uid, 표시이름). 실패 시 (None, 사유)."""
    q = str(query).strip()
    if q.lstrip("-").isdigit():
        return int(q), q
    if not q.startswith("@"):
        q = "@" + q
    res = _tg("getChat", chat_id=q)
    if not res.get("ok"):
        return None, res.get("description", "사용자를 찾을 수 없습니다")
    d = res.get("result", {})
    if d.get("type") != "private":
        return None, "사용자 계정이 아닙니다"
    name = (d.get("first_name") or "")
    if d.get("last_name"):
        name += " " + d["last_name"]
    return d.get("id"), (name.strip() or q)
