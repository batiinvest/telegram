# -*- coding: utf-8 -*-
"""광고/홍보 메시지 모더레이션.

정책: 고신뢰 광고·도배는 **자동 삭제**, **차단은 관리자 버튼 확인 후**.
탐지(종목방 오탐 최소화 — 키워드 의존 회피):
  ① 텔레그램 외부 초대링크 (t.me/+, joinchat) — 글자에 숨긴 링크·버튼 링크 포함
  ② 카카오 오픈채팅 링크
  ③ 도배(같은 문구, 10분 안)
     · 한 사람이 같은 방에 3번 이상                       → 자동 삭제
     · 여러 계정이 같은 문구를 3개 방 이상(뉴스 링크 제외) → 자동 삭제
     · 한 계정이 같은 문구를 3개 방 이상(뉴스 링크 제외)   → 알림만(삭제 버튼)
   같은 문구는 묶음(burst) 하나로 관리자 알림 1건에 모으고, 계정·방이 늘면 그 알림을 수정,
   처리 안 된 채 계속되면 30분마다 새로 알린다. 수정된 메시지는 ①②만 검사한다.
제외: 봇, 각 방 관리자(캐시), 화이트리스트(관리자 본인·스태프).
실제 차단은 봇 banChatMember (관리자 콜백 SPAM|ban/banall/fban).
"""
import os
import re
import time
import threading
import html as _html
import hashlib
import logging
from datetime import datetime, timezone, timedelta

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

_KST = timezone(timedelta(hours=9))

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

# 뉴스/정상 도메인 — 다중방 도배 판정에서 제외 (정상 뉴스 공유 오탐 방지)
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


# 도배 추적: text_hash -> [(chat_id, ts, uid, message_id, 표시이름)]
_recent = {}
_FLOOD_WINDOW = 600     # 10분
_FLOOD_ROOMS = 3        # 3개 방 이상
_FLOOD_REPEAT = 3       # 같은 사람·같은 방 반복 횟수
_FLOOD_MINLEN = 12

# 판정 종류 → (사유, 자동 삭제)
_REASONS = {
    "repeat": ("같은 방 반복 도배", True),
    "multi": ("여러 계정 다중방 도배", True),
    "rooms": ("다중방 도배(의심)", False),
}
_FLOOD_REASONS = {r for r, _ in _REASONS.values()}

# 방별 관리자 캐시: chat_id -> (set(ids), ts)
_admin_cache = {}
_ADMIN_TTL = 3600

# 도배 묶음 — 같은 문구는 관리자 알림 1건으로 모으고, 버튼(SPAM|fban/fdel/fok|key)이 이 기록으로 처리한다.
_bursts = {}           # text_hash -> 묶음 dict (_new_burst)
_burst_keys = {}       # 버튼 key -> text_hash
_ok_until = {}         # text_hash -> 이 시각까지 '정상' 처리(삭제·알림 안 함)
_room_titles = {}      # chat_id -> 표시용 방 제목(최근값)
_REALERT_SEC = 1800    # 처리 안 된 묶음이 계속되면 30분마다 새 알림
_EDIT_MIN_SEC = 60     # 건수만 늘 때 알림 수정 최소 간격
_OK_TTL = 6 * 3600
_BURST_KEEP = 24 * 3600  # 버튼이 동작하도록 묶음 기록 보관
_LOCK = threading.RLock()

_tls = threading.local()


def _session():
    s = getattr(_tls, "s", None)
    if s is None:
        s = _tls.s = requests.Session()   # 스레드별 연결 재사용 — 79개 방 차단 70초→12초
    return s


def _tg(method, **params):
    if not _BOT_TOKEN:
        return {}
    res = {}
    for _ in range(3):
        try:
            res = _session().post(f"https://api.telegram.org/bot{_BOT_TOKEN}/{method}",
                                  json=params, timeout=10).json()
        except Exception as e:
            log.debug(f"[spam][tg] {method} 오류: {e}")
            return {}
        if res.get("error_code") != 429:
            return res
        wait = int((res.get("parameters") or {}).get("retry_after", 1))
        time.sleep(min(wait, 30) + 0.5)
    return res


def _esc(s):
    return _html.escape(str(s or ""), quote=False)


def _text_hash(text):
    return hashlib.md5(text.strip().encode("utf-8", "ignore")).hexdigest()


def _hm(ts):
    return datetime.fromtimestamp(ts, _KST).strftime("%H:%M")


def _who(frm):
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
    if frm.get("username"):
        name += " @" + frm["username"]
    return name.strip()


def _hidden_urls(message):
    """글자에 숨긴 링크(text_link)·버튼 링크 — 화면 글자엔 안 보이는 URL."""
    out = []
    for ent in (message.get("entities") or []) + (message.get("caption_entities") or []):
        if ent.get("type") == "text_link" and ent.get("url"):
            out.append(ent["url"])
    for row in ((message.get("reply_markup") or {}).get("inline_keyboard") or []):
        for btn in row:
            if btn.get("url"):
                out.append(btn["url"])
    return out


def _record(h, chat_id, uid, msg_id, name, now):
    lst = [e for e in _recent.pop(h, []) if now - e[1] < _FLOOD_WINDOW]
    lst.append((chat_id, now, uid, msg_id, name))
    _recent[h] = lst                 # pop 후 다시 넣어 최근 순서 유지
    if len(_recent) > 2000:          # 메모리 가드 — 만료분 먼저, 그래도 많으면 오래된 것부터
        for k in [k for k, v in _recent.items() if now - v[-1][1] >= _FLOOD_WINDOW]:
            _recent.pop(k, None)
        for k in list(_recent)[:max(0, len(_recent) - 1000)]:
            _recent.pop(k, None)
    return lst


def _flood_kind(text, chat_id, uid=None, msg_id=None, name=""):
    """같은 문구 도배 판정 → 'repeat'·'multi'·'rooms'·None (_REASONS 참고)."""
    t = text.strip()
    if len(t) < _FLOOD_MINLEN:
        return None
    h = _text_hash(t)
    now = time.time()
    with _LOCK:
        lst = _record(h, chat_id, uid, msg_id, name, now)
        if _ok_until.get(h, 0) > now:
            return None
    if uid is not None and sum(1 for e in lst if e[0] == chat_id and e[2] == uid) >= _FLOOD_REPEAT:
        return "repeat"
    if len({e[0] for e in lst}) < _FLOOD_ROOMS or _has_news_link(t):
        return None
    senders = {e[2] for e in lst if e[2] is not None}
    return "multi" if len(senders) >= 2 else "rooms"


def _detect(text, chat_id, uid=None, msg_id=None, name="", links="", track=True):
    """(is_spam, reason, auto_delete). links=숨은 링크(①② 검사에만), track=False면 도배 집계 안 함."""
    full = (text + " " + links) if links else text
    if _INVITE_RE.search(full):
        return True, "텔레그램 외부 초대링크", True
    if _KAKAO_RE.search(full):
        return True, "카카오 오픈채팅", True
    if track and text:
        kind = _flood_kind(text, chat_id, uid, msg_id, name)
        if kind:
            reason, auto = _REASONS[kind]
            return True, reason, auto
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


def _delete(chat_id, msg_id):
    """메시지 삭제 — 이미 지워진 메시지도 성공으로 본다."""
    if not msg_id:
        return False
    res = _tg("deleteMessage", chat_id=chat_id, message_id=msg_id)
    return bool(res.get("ok")) or "not found" in (res.get("description") or "")


def check_message(scanner, message, edited=False):
    """그룹 메시지 검사 → 광고(자동삭제)·도배(고신뢰 자동삭제/알림) → 관리자 알림.
    edited=True(수정된 메시지)는 링크(①②)만 본다 — 같은 메시지라 도배 집계엔 넣지 않는다."""
    chat = message.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return
    text = message.get("text") or message.get("caption") or ""
    links = " ".join(_hidden_urls(message))
    if not text and not links:
        return
    frm = message.get("from", {})
    uid = frm.get("id")
    if not uid or frm.get("is_bot") or uid in _exempt_ids():
        return
    chat_id = chat.get("id")
    _remember_room_title(chat_id, chat.get("title"))
    msg_id = message.get("message_id")
    is_spam, reason, auto_delete = _detect(text, chat_id, uid, msg_id, _who(frm), links, track=not edited)
    if not is_spam:
        return
    if _is_chat_admin(chat_id, uid):
        return  # 관리자 발언은 제외
    if reason in _FLOOD_REASONS:
        reason, auto_delete = _handle_flood(text, reason, auto_delete)
        log.info(f"[spam] {'삭제' if auto_delete else '알림'} chat={chat_id} uid={uid} reason={reason}")
        return
    ok = _delete(chat_id, msg_id)
    _notify_admin(chat, frm, text, reason, ok, msg_id, edited, links)
    _act = '삭제' if ok else '삭제 실패'
    log.info(f"[spam] {_act} chat={chat_id} uid={uid} reason={reason}{' (수정됨)' if edited else ''}")


def _notify_admin(chat, frm, text, reason, deleted, message_id, edited=False, links=""):
    admin = _get_admin_chat()
    if not admin:
        return
    _NL = chr(10)
    uid = frm.get("id")
    cid = chat.get("id")
    title = chat.get("title") or str(cid)
    head = "🚫 <b>광고 의심 — 자동 삭제됨</b>" if deleted else "⚠️ <b>광고 의심 — 자동 삭제 실패(봇 권한 확인)</b>"
    if edited:
        head += " (수정된 메시지)"
    body = (head + _NL + _NL
            + "방: <b>" + _esc(title) + "</b>" + _NL
            + "발신: " + _esc(_who(frm)) + " (id <code>" + str(uid) + "</code>)" + _NL
            + "사유: " + reason + _NL
            + "내용: " + _esc(text[:300]))
    if links:
        body += _NL + "숨은 링크: " + _esc(links[:200])
    row = []
    if not deleted:
        row.append({"text": "🗑 삭제", "callback_data": "SPAM|del|" + str(cid) + "|" + str(message_id)})
    row.append({"text": "🚫 이 방 차단", "callback_data": "SPAM|ban|" + str(uid) + "|" + str(cid)})
    row.append({"text": "🚫 전체 차단", "callback_data": "SPAM|banall|" + str(uid)})
    row.append({"text": "✅ 정상", "callback_data": "SPAM|ok"})
    _tg("sendMessage", chat_id=admin, parse_mode="HTML", text=body,
        reply_markup={"inline_keyboard": [row]}, disable_web_page_preview=True)


def _remember_room_title(cid, title):
    if cid is None:
        return
    _room_titles[cid] = title or str(cid)
    if len(_room_titles) > 3000:
        for k in list(_room_titles)[:1500]:
            _room_titles.pop(k, None)


# ── 도배 묶음 ──────────────────────────────────────────────
def _new_burst(h, text, now):
    key = h[:8] + format(int(now) % 0xFFFFFF, "06x")
    b = {"hash": h, "key": key, "text": text.strip(), "first": now, "last": now,
         "msgs": [], "seen": set(), "deleted": set(), "failed": set(),
         "users": {}, "rooms": [], "banned": set(),
         "auto": False, "reason": "", "handled": None, "status": "",
         "mid": None, "mids": [], "sent_ts": 0, "edit_ts": 0, "n_shown": 0}
    for k, v in list(_bursts.items()):
        if now - v["last"] > _BURST_KEEP:
            _bursts.pop(k, None)
            _burst_keys.pop(v["key"], None)
    for k in [k for k, t in _ok_until.items() if t < now]:
        _ok_until.pop(k, None)
    _bursts[h] = b
    _burst_keys[key] = h
    return b


def _handle_flood(text, reason, auto):
    """도배 메시지를 묶음에 넣고, 자동 삭제 대상이면 묶음의 남은 메시지까지 지운 뒤 관리자 알림 갱신."""
    h = _text_hash(text)
    now = time.time()
    with _LOCK:
        entries = list(_recent.get(h, []))
    # 방 관리자 메시지는 묶음에서 뺀다(스태프 공지를 따라 쓴 경우 등)
    entries = [e for e in entries if e[2] is not None and not _is_chat_admin(e[0], e[2])]
    if reason == _REASONS["multi"][0] and len({e[2] for e in entries}) < 2:
        reason, auto = _REASONS["rooms"]
    with _LOCK:
        b = _bursts.get(h)
        if b is None or now - b["last"] > _FLOOD_WINDOW:
            b = _new_burst(h, text, now)
        changed = False
        for c, ts, u, m, nm in entries:
            b["first"] = min(b["first"], ts)
            if (c, m) not in b["seen"]:
                b["seen"].add((c, m))
                b["msgs"].append((c, m, u))
            if u not in b["users"]:
                b["users"][u] = nm
                changed = True
                if b["handled"] == "ban" and u not in b["banned"]:
                    b["handled"] = None      # 차단 뒤 새 계정 등장 → 다시 알림 대상
            if c not in b["rooms"]:
                b["rooms"].append(c)
                changed = True
        b["last"] = now
        if auto and not b["auto"]:
            b["auto"] = True
            changed = True
        if auto or not b["auto"]:
            b["reason"] = reason
        todo = _pending_deletes(b) if b["auto"] else []
    _delete_many(b, todo)
    _notify_burst(b, changed)
    return b["reason"], b["auto"]


def _pending_deletes(b):
    return [(c, m) for c, m, _u in b["msgs"] if (c, m) not in b["deleted"] and (c, m) not in b["failed"]]


def _delete_many(b, pairs):
    for c, m in pairs:
        ok = _delete(c, m)
        with _LOCK:
            (b["deleted"] if ok else b["failed"]).add((c, m))


def _burst_body(b):
    _NL = chr(10)
    users = list(b["users"].items())
    lines = ["· " + _esc(nm or "?") + " (id <code>" + str(u) + "</code>)" + (" 🚫" if u in b["banned"] else "")
             for u, nm in users[:10]]
    if len(users) > 10:
        lines.append(f"· 외 {len(users) - 10}명")
    names = [_room_titles.get(c, str(c)) for c in b["rooms"]]
    room_line = ", ".join(_esc(x) for x in names[:15]) + (f" 외 {len(names) - 15}곳" if len(names) > 15 else "")
    cnt = f"메시지 {len(b['msgs'])}건 · 삭제 {len(b['deleted'])}건"
    if b["failed"]:
        cnt += f" · 삭제 실패 {len(b['failed'])}건"
    if b["handled"] == "ok":
        head = "✅ <b>정상 처리 — 도배 아님</b>"
    elif b["auto"]:
        head = "🧹 <b>도배 스팸 — 자동 삭제 중</b>"
    else:
        head = "⚠️ <b>도배 의심 — 삭제 안 함</b>"
    body = (head + _NL + _NL
            + f"계정 {len(users)}명:" + _NL + _NL.join(lines) + _NL
            + f"방 {len(names)}곳: " + room_line + _NL
            + cnt + _NL
            + "사유: " + b["reason"] + _NL
            + "시간: " + _hm(b["first"]) + " ~ " + _hm(b["last"]) + _NL
            + "내용: " + _esc(b["text"][:300]))
    if b["status"]:
        body += _NL + _NL + b["status"]
    if b["handled"] == "ok":
        return body, {"inline_keyboard": []}
    row = []
    if not b["auto"]:
        row.append({"text": "🗑 삭제만", "callback_data": "SPAM|fdel|" + b["key"]})
    row.append({"text": "🚫 전원 차단·삭제", "callback_data": "SPAM|fban|" + b["key"]})
    row.append({"text": "✅ 정상", "callback_data": "SPAM|fok|" + b["key"]})
    return body, {"inline_keyboard": [row]}


def _notify_burst(b, changed=False):
    """첫 알림·30분 재알림은 새 메시지, 계정·방이 늘거나 건수가 바뀌면(1분 간격) 기존 알림 수정."""
    admin = _get_admin_chat()
    if not admin:
        return
    now = time.time()
    with _LOCK:
        n = len(b["msgs"])
        if b["mid"] is None or (b["handled"] is None and now - b["sent_ts"] >= _REALERT_SEC):
            action = "send"
            b["sent_ts"] = now
        elif changed or (n != b["n_shown"] and now - b["edit_ts"] >= _EDIT_MIN_SEC):
            action = "edit"
        else:
            return
        b["n_shown"] = n
        b["edit_ts"] = now
        body, kb = _burst_body(b)
        mid = b["mid"]
    if action == "send":
        res = _tg("sendMessage", chat_id=admin, parse_mode="HTML", text=body,
                  reply_markup=kb, disable_web_page_preview=True)
        new_mid = (res.get("result") or {}).get("message_id")
        if new_mid:
            with _LOCK:
                b["mid"] = new_mid
                b["mids"].append(new_mid)
    else:
        _tg("editMessageText", chat_id=admin, message_id=mid, parse_mode="HTML",
            text=body, reply_markup=kb, disable_web_page_preview=True)


def _refresh_alerts(b, extra_mid=None):
    """버튼 처리 결과를 이 묶음의 알림(재알림 포함) 전부에 반영."""
    admin = _get_admin_chat()
    if not admin:
        return
    with _LOCK:
        body, kb = _burst_body(b)
        mids = list(b["mids"])
    if extra_mid and extra_mid not in mids:
        mids.append(extra_mid)
    for mid in mids:
        _tg("editMessageText", chat_id=admin, message_id=mid, parse_mode="HTML",
            text=body, reply_markup=kb, disable_web_page_preview=True)


def flood_action(key, action, clicked_mid=None):
    """도배 알림 버튼: 'ban'(전원 전체 차단+남은 메시지 삭제)·'del'(삭제만)·'ok'(정상). 결과 문구 반환.
    차단은 79개 방 × 계정 수만큼 호출하므로 리스너가 아닌 별도 스레드에서 부른다."""
    with _LOCK:
        h = _burst_keys.get(key)
        b = _bursts.get(h) if h else None
    if not b:
        return "기록이 없습니다(봇 재시작 등) — 알림의 id로 /차단 하세요"
    if action == "ok":
        with _LOCK:
            b["handled"] = "ok"
            b["auto"] = False
            _ok_until[b["hash"]] = time.time() + _OK_TTL
            b["status"] = "이 문구는 6시간 동안 삭제·알림하지 않습니다"
        _refresh_alerts(b, clicked_mid)
        log.info(f"[spam] 정상 처리 key={key}")
        return "정상 처리 — 6시간 동안 삭제·알림 안 함"
    with _LOCK:
        b["auto"] = True                 # 관리자가 스팸으로 확인 → 이후 같은 문구는 자동 삭제
        uids = [u for u in b["users"] if u not in b["banned"]] if action == "ban" else []
        todo = _pending_deletes(b)
    status = []
    if uids:
        res, nrooms = ban_many(uids)
        with _LOCK:
            b["banned"].update(u for u, k in res.items() if k)
            b["handled"] = "ban"
        status.append(f"🚫 {sum(1 for k in res.values() if k)}명 전체 차단({nrooms}개 방)")
    _delete_many(b, todo)
    with _LOCK:
        status.append(f"🗑 메시지 {len(b['deleted'])}건 삭제됨")
        b["status"] = " · ".join(status)
    _refresh_alerts(b, clicked_mid)
    log.info(f"[spam] 버튼 {action} key={key} → {b['status']}")
    return b["status"]


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


def ban_many(uids):
    """여러 계정을 모든 방(산업 포함)에서 차단. ({uid: 성공 방 수}, 전체 방 수)."""
    cids = _all_room_cids()
    res = {int(u): 0 for u in uids}
    for cid in cids:
        for u in res:
            if _tg("banChatMember", chat_id=cid, user_id=u).get("ok"):
                res[u] += 1
    return res, len(cids)


def ban_all(uid):
    """모든 방(산업 포함)에서 차단. 성공 방 수 반환."""
    return ban_many([uid])[0][int(uid)]


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
