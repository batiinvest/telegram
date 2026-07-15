# -*- coding: utf-8 -*-
"""미접속(last-seen) 멤버 강퇴 (Telethon 유저 클라이언트).

봇 메뉴(/미접속)에서 방 목록을 보고 방을 선택 → 명단 확인 → 강퇴한다.

판정:
  - UserStatusOffline.was_online 이 (now - INACTIVE_DAYS) 이전        -> 대상
  - UserStatusLastMonth (지난달)                                      -> 대상
  - 삭제된 계정(user.deleted)                                          -> 대상(정리)
  - Recently / LastWeek / Online / 숨김(Empty/None)                   -> 유지(안전)
제외: 관리자/봇/본인.
방식: kick_participant(ban->unban) = 재입장 가능. FloodWait 자동 대기.
강퇴 직전 last-seen 재확인(스캔 후 활동 재개자 제외).
대상 방: rooms.chat_id 가 숫자(-100…)인 전체 방.
"""
import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from supabase import create_client
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    UserStatusOffline, UserStatusLastMonth,
)
from telethon.tl.types import ChannelParticipantsAdmins

load_dotenv("/home/kjhofone/.env")

INACTIVE_DAYS = 5
KICK_SLEEP = 1.5          # 강퇴 간 간격(초) — 계정 보호
CACHE_TTL_MIN = 30        # 스캔 결과 재사용 시간(분)
SAMPLE_N = 8              # 명단 미리보기 개수
REPORT_PATH = "/home/kjhofone/logs/inactive_kick_report.txt"

_API_ID = int(os.environ["TELETHON_API_ID"])
_API_HASH = os.environ["TELETHON_API_HASH"]
_SESSION = os.environ["TELETHON_SESSION"]
_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# 마지막 스캔 후보 캐시 (메뉴 강퇴에 사용)
_LAST = {"ts": None, "rooms": {}, "total": 0}
_LOCK = threading.Lock()


def _sb():
    return create_client(os.environ["SB_URL"], os.environ["SB_SERVICE_KEY"])


def _target_rooms():
    rows = _sb().table("rooms").select("id,name,chat_id,status,members") \
        .order("status").order("name").execute().data or []
    return [r for r in rows if str(r.get("chat_id") or "").lstrip("-").isdigit()]


def _should_kick(user, cutoff):
    """(kick?, reason)"""
    if getattr(user, "deleted", False):
        return True, "deleted"
    if getattr(user, "bot", False):
        return False, ""
    st = user.status
    if isinstance(st, UserStatusOffline):
        wo = st.was_online
        if wo is not None and wo < cutoff:
            return True, "offline " + wo.strftime("%Y-%m-%d")
        return False, ""
    if isinstance(st, UserStatusLastMonth):
        return True, "lastmonth"
    return False, ""


def _display_name(u):
    if getattr(u, "deleted", False):
        return "(삭제계정)"
    nm = (u.first_name or "")
    if u.last_name:
        nm += " " + u.last_name
    nm = nm.strip()
    if nm:
        return nm
    if u.username:
        return "@" + u.username
    return str(u.id)


def _exempt_ids():
    """강퇴 전역 예외(관리자/스태프). .env KICK_EXEMPT_IDS(콤마) + 운영계정."""
    ids = set()
    raw = os.environ.get("KICK_EXEMPT_IDS", "")
    for x in raw.replace(" ", "").split(","):
        if x.lstrip("-").isdigit():
            ids.add(int(x))
    return ids


def _scan_room(client, room, cutoff, me_id):
    """단일 방 스캔 → (seen, [uid,...], [name 샘플]). 권한없음/오류 시 예외."""
    cid = int(room["chat_id"])
    perm = client.get_permissions(cid, "me")
    if not (perm.is_admin and perm.ban_users):
        raise PermissionError("no ban rights")
    admin_ids = {a.id for a in client.iter_participants(
        cid, filter=ChannelParticipantsAdmins)}
    exempt = _exempt_ids()
    cands = []
    samples = []
    seen = 0
    for u in client.iter_participants(cid):
        seen += 1
        if u.id == me_id or u.id in admin_ids or u.id in exempt:
            continue
        ok, _ = _should_kick(u, cutoff)
        if ok:
            cands.append(u.id)
            if len(samples) < SAMPLE_N:
                samples.append(_display_name(u))
    return seen, cands, samples


def scan_candidates():
    """전체 방 스캔 → 후보 캐시 갱신. (rooms_dict, total)."""
    rooms = _target_rooms()
    cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
    out = {}
    total = 0
    with TelegramClient(StringSession(_SESSION), _API_ID, _API_HASH) as client:
        me_id = client.get_me().id
        for r in rooms:
            try:
                seen, cands, samples = _scan_room(client, r, cutoff, me_id)
            except Exception:
                continue
            out[r["id"]] = {
                "name": r["name"], "status": r["status"],
                "chat_id": r["chat_id"], "cands": cands,
                "samples": samples, "seen": seen,
            }
            total += len(cands)
    with _LOCK:
        _LAST["ts"] = datetime.now(timezone.utc)
        _LAST["rooms"] = out
        _LAST["total"] = total
    return out, total


def get_cache():
    with _LOCK:
        return dict(_LAST["rooms"]), _LAST["total"], _LAST["ts"]


def cache_age_min():
    with _LOCK:
        ts = _LAST["ts"]
    if not ts:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def cache_fresh():
    with _LOCK:
        has = bool(_LAST["rooms"])
    a = cache_age_min()
    return has and a is not None and a < CACHE_TTL_MIN


def room_brief(room_id):
    """확인창용 (name, count, [samples], is_paid)."""
    with _LOCK:
        rooms = dict(_LAST["rooms"])
    if room_id == "all":
        total = sum(len(v["cands"]) for v in rooms.values())
        return "전체", total, [], False
    info = rooms.get(int(room_id)) if str(room_id).isdigit() else None
    if not info:
        return "", 0, [], False
    return info["name"], len(info["cands"]), info.get("samples", []), (info.get("status") == "paid")


def kick_cached(room_id):
    """캐시된 후보를 강퇴(강퇴 직전 last-seen 재확인). (done, total, name)."""
    with _LOCK:
        rooms = dict(_LAST["rooms"])
    if not rooms:
        return 0, 0, ""
    if room_id == "all":
        targets = list(rooms.items())
        name = "전체"
    else:
        rid = int(room_id)
        info = rooms.get(rid)
        targets = [(rid, info)] if info else []
        name = info["name"] if info else ""
    cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
    done = total = 0
    with TelegramClient(StringSession(_SESSION), _API_ID, _API_HASH) as client:
        for rid, info in targets:
            if not info:
                continue
            cid = int(info["chat_id"])
            cand_set = set(info["cands"])
            total += len(cand_set)
            if cand_set:
                # 참가자 순회로 entity 확보 + 강퇴 직전 재확인
                for u in client.iter_participants(cid):
                    if u.id not in cand_set:
                        continue
                    if not _should_kick(u, cutoff)[0]:
                        continue  # 스캔 후 활동 재개 → 제외
                    try:
                        client.kick_participant(cid, u)
                        done += 1
                        time.sleep(KICK_SLEEP)
                    except FloodWaitError as fw:
                        time.sleep(fw.seconds + 2)
                        try:
                            client.kick_participant(cid, u)
                            done += 1
                        except Exception:
                            pass
                    except Exception:
                        pass
            # 강퇴 끝난 방은 캐시 비워 재클릭 중복 방지
            with _LOCK:
                if rid in _LAST["rooms"]:
                    _LAST["rooms"][rid]["cands"] = []
                    _LAST["rooms"][rid]["samples"] = []
                _LAST["total"] = sum(len(v["cands"]) for v in _LAST["rooms"].values())
    return done, total, name


# ── 전체 스캔+강퇴 (CLI / 스케줄용) ─────────────────────────────
def scan_and_kick(dry_run=True):
    rooms = _target_rooms()
    cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
    started = datetime.now(timezone.utc)
    lines = []
    total_seen = total_kick = total_done = 0
    with TelegramClient(StringSession(_SESSION), _API_ID, _API_HASH) as client:
        me_id = client.get_me().id
        for r in rooms:
            tag = f"{r['name']}({r['status']})"
            try:
                seen, cands, _ = _scan_room(client, r, cutoff, me_id)
            except Exception as e:
                lines.append(f"❌ {tag}: {type(e).__name__}")
                continue
            total_seen += seen
            total_kick += len(cands)
            if not dry_run:
                cid = int(r["chat_id"])
                for uid in cands:
                    try:
                        client.kick_participant(cid, uid)
                        total_done += 1
                        time.sleep(KICK_SLEEP)
                    except FloodWaitError as fw:
                        time.sleep(fw.seconds + 2)
                        try:
                            client.kick_participant(cid, uid)
                            total_done += 1
                        except Exception:
                            pass
                    except Exception:
                        pass
            mark = "🔸" if cands else "✅"
            lines.append(f"{mark} {tag}: {len(cands)}명 / {seen}명")
    mode = "DRY-RUN" if dry_run else "LIVE"
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    header = (
        f"📋 미접속({INACTIVE_DAYS}일) 강퇴 — {mode}\n"
        f"방 {len(rooms)}개 / 스캔 {total_seen}명 / 대상 {total_kick}명"
        + (f" / 강퇴 {total_done}명" if not dry_run else "")
        + f" / {elapsed:.0f}s\n"
    )
    report = header + "\n".join(lines)
    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        pass
    return report, total_kick, total_done


def send_admin(text):
    try:
        from telegram_utils import get_admin_chat_id
        admin = get_admin_chat_id()
    except Exception:
        admin = os.environ.get("ADMIN_CHAT_ID", "")
    if not admin or not _BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    buf = ""
    for ln in text.split("\n"):
        if len(buf) + len(ln) + 1 > 3800:
            requests.post(url, json={"chat_id": admin, "text": buf}, timeout=15)
            buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf:
        requests.post(url, json={"chat_id": admin, "text": buf}, timeout=15)


def job_inactive_kick():
    """스케줄러용 — 전체 실제 강퇴 + 관리자 리포트."""
    report, _, _ = scan_and_kick(dry_run=False)
    send_admin(report)


if __name__ == "__main__":
    live = "--live" in sys.argv
    rpt, tkick, tdone = scan_and_kick(dry_run=not live)
    print(rpt)
    if "--notify" in sys.argv:
        send_admin(rpt)
