"""
room_access.py — 종목 유료방 1회 입장 관리
────────────────────────────────────────────
`rooms` 테이블에서 status='paid' 인 종목방에 대해, Litt.ly 결제 후
구매자가 봇을 통해 입장 신청 → 어드민 1클릭 승인 → 1회용 초대 링크
발급까지를 처리합니다.

구독(pro_channel.py / pro_members)과는 **완전 별개**입니다.
1회 입장료이므로 만료·자동퇴장 개념이 없습니다.

흐름:
  1. 구매자가 Litt.ly 결제 후 t.me/<bot>?start=paidroom 진입
     → bot_commands._handle 이 start_entry() 호출 → 방 선택 버튼 발송
  2. 구매자가 방 선택(ROOMSEL) → realtime_alert 가 request_room() 호출
     → room_entries(status='pending') 기록 + 어드민에 승인 버튼 알림
  3. 어드민이 Litt.ly 주문(@username)과 대조 후 ✅ 승인(ROOM|approve)
     → realtime_alert 가 approve_room() 호출 → 1회용 링크 DM + status='approved'

DB:
  room_entries 테이블 (room_entries.sql 로 생성)
"""

import time
import requests
from typing import Optional
from logger_config import get_logger

log = get_logger(__name__)

try:
    from config import TELEGRAM_BOT_TOKEN as _BOT_TOKEN
except ImportError:
    _BOT_TOKEN = None

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except ImportError:
    _BRIDGE_OK = False
    log.warning("supabase_bridge 없음 — room_access 비활성화")

from telegram_utils import get_admin_chat_id as _get_admin_chat

_TG_API = "https://api.telegram.org/bot{token}/{method}"
_INVITE_EXPIRE_HOURS = 3    # 1회용 링크 미사용 시 자동 만료


# ══════════════════════════════════════════════════════════════
# 🛠️  내부 헬퍼
# ══════════════════════════════════════════════════════════════

def _tg(method: str, **params) -> dict:
    """Telegram Bot API 호출. 실패 시 {} 반환."""
    if not _BOT_TOKEN:
        log.error("[room] BOT_TOKEN 없음")
        return {}
    url = _TG_API.format(token=_BOT_TOKEN, method=method)
    try:
        r = requests.post(url, json=params, timeout=10)
        data = r.json()
        if not data.get('ok'):
            log.warning(f"[room][TG] {method} 실패: {data.get('description')}")
        return data
    except Exception as e:
        log.error(f"[room][TG] {method} 오류: {e}")
        return {}


def _sb():
    """Supabase 클라이언트. 없으면 None."""
    if not _BRIDGE_OK:
        return None
    return _bridge._get_client()


# ══════════════════════════════════════════════════════════════
# 📋  유료방 조회
# ══════════════════════════════════════════════════════════════

def get_paid_rooms() -> list:
    """rooms 테이블에서 status='paid' 이고 chat_id 가 있는 방 목록."""
    sb = _sb()
    if not sb:
        return []
    try:
        res = sb.table('rooms') \
                .select('id,name,chat_id,link,cat,status') \
                .eq('status', 'paid').order('name').execute()
        return [r for r in (res.data or []) if r.get('chat_id')]
    except Exception as e:
        log.error(f"[room] 유료방 조회 실패: {e}")
        return []


def _get_room(room_id) -> Optional[dict]:
    """단일 방 조회."""
    sb = _sb()
    if not sb:
        return None
    try:
        res = sb.table('rooms') \
                .select('id,name,chat_id,status').eq('id', room_id).single().execute()
        return res.data
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# 🔗  1회용 초대 링크
# ══════════════════════════════════════════════════════════════

def create_invite_link(chat_id: str, expire_hours: int = _INVITE_EXPIRE_HOURS) -> Optional[str]:
    """
    특정 방의 1회용·자동만료 초대 링크 생성.
    member_limit=1 → 1명 입장 시 자동 소멸 / expire_date → 미사용 시 만료.
    (봇이 해당 방의 관리자여야 함)
    """
    expire_ts = int(time.time()) + expire_hours * 3600
    res = _tg('createChatInviteLink',
              chat_id=chat_id, member_limit=1, expire_date=expire_ts)
    if not res.get('ok'):
        return None
    return res.get('result', {}).get('invite_link')


# ══════════════════════════════════════════════════════════════
# ① 입장 안내 — 방 선택 버튼 발송 (bot_commands /start paidroom)
# ══════════════════════════════════════════════════════════════

def start_entry(uid: int, username: str = '', name: str = '') -> bool:
    """결제 후 봇에 진입한 구매자에게 유료방 선택 버튼을 DM으로 발송."""
    rooms = get_paid_rooms()
    if not rooms:
        _tg('sendMessage', chat_id=uid, parse_mode='HTML',
            text="현재 입장 가능한 유료 종목방이 없습니다.\n문의: @batiinvest")
        return False

    keyboard = {'inline_keyboard': [
        [{'text': f"🔒 {r['name']}", 'callback_data': f"ROOMSEL|{r['id']}"}]
        for r in rooms
    ]}
    res = _tg('sendMessage', chat_id=uid, parse_mode='HTML',
              text=(
                  "🎟 <b>종목 유료방 입장</b>\n\n"
                  "입장하실 종목방을 선택해 주세요.\n"
                  "후원(litt.ly/batiinvest) 확인 후 <b>1회용 입장 링크</b>를 보내드립니다."
              ),
              reply_markup=keyboard)
    return res.get('ok', False)


# ══════════════════════════════════════════════════════════════
# ② 입장 신청 — 구매자가 방 선택 (ROOMSEL)
# ══════════════════════════════════════════════════════════════

def request_room(uid: int, username: str, name: str, room_id) -> bool:
    """구매자의 방 선택을 room_entries(pending)에 기록하고 어드민에 승인 요청."""
    sb = _sb()
    if not sb:
        return False

    room = _get_room(room_id)
    if not room:
        _tg('sendMessage', chat_id=uid, parse_mode='HTML',
            text="선택하신 방을 찾을 수 없습니다. 문의: @batiinvest")
        return False

    # 이미 승인된 방이면 중복 신청 차단
    try:
        dup = sb.table('room_entries').select('id') \
                .eq('telegram_id', uid).eq('room_id', room_id) \
                .eq('status', 'approved').execute()
        if dup.data:
            _tg('sendMessage', chat_id=uid, parse_mode='HTML',
                text=(f"이미 <b>{room['name']}</b> 입장이 승인된 계정입니다.\n"
                      f"링크 분실 시 @batiinvest로 문의해 주세요."))
            return False
    except Exception as e:
        log.debug(f"[room] 중복 확인 오류(무시): {e}")

    # pending 기록
    try:
        sb.table('room_entries').insert({
            'telegram_id':       uid,
            'telegram_username': ('@' + username) if username else '',
            'telegram_name':     name,
            'room_id':           room['id'],
            'room_name':         room['name'],
            'room_chat_id':      room['chat_id'],
            'status':            'pending',
        }).execute()
    except Exception as e:
        log.error(f"[room] 신청 기록 실패: {e}")
        _tg('sendMessage', chat_id=uid, text="처리 중 오류가 발생했습니다. 문의: @batiinvest")
        return False

    _tg('sendMessage', chat_id=uid, parse_mode='HTML',
        text=(f"✅ <b>{room['name']}</b> 입장 신청이 접수되었습니다.\n"
              f"결제 확인 후 입장 링크를 보내드릴게요. 잠시만 기다려 주세요."))

    _notify_admin(uid, username, name, room)
    log.info(f"[room] 입장 신청: {uid}(@{username}) → {room['name']}")
    return True


def _notify_admin(uid: int, username: str, name: str, room: dict):
    """어드민에게 승인/거절 버튼 알림 발송."""
    admin = _get_admin_chat()
    if not admin:
        log.warning("[room] admin_chat_id 미설정 — 입장 신청 알림 불가")
        return
    uname = f"@{username}" if username else "없음"
    msg = (
        f"🎟 <b>[유료방 입장 신청]</b>\n\n"
        f"방: <b>{room['name']}</b>\n"
        f"이름: {name or '-'}\n"
        f"@username: {uname}\n"
        f"텔레그램 ID: <code>{uid}</code>\n\n"
        f"👉 Litt.ly 주문(@username)과 대조 후 승인하세요."
    )
    keyboard = {'inline_keyboard': [[
        {'text': '✅ 승인 + 입장링크', 'callback_data': f"ROOM|approve|{uid}|{room['id']}"},
        {'text': '❌ 거절',            'callback_data': f"ROOM|reject|{uid}|{room['id']}"},
    ]]}
    _tg('sendMessage', chat_id=admin, text=msg, parse_mode='HTML', reply_markup=keyboard)


# ══════════════════════════════════════════════════════════════
# ③ 승인 / 거절 (어드민 콜백)
# ══════════════════════════════════════════════════════════════

def approve_room(uid: int, room_id, admin_id: str = '') -> tuple[bool, str]:
    """승인 → 1회용 링크 DM + room_entries status='approved'. (ok, 메시지) 반환."""
    room = _get_room(room_id)
    if not room:
        return False, "방을 찾을 수 없습니다."

    link = create_invite_link(room['chat_id'])
    if not link:
        return False, "초대 링크 생성 실패 — 봇이 해당 방 관리자인지 확인하세요."

    res = _tg('sendMessage', chat_id=uid, parse_mode='HTML',
              text=(
                  f"🔓 <b>{room['name']}</b> 입장 링크입니다.\n\n"
                  f"🔗 {link}\n\n"
                  f"⚠️ 1회용 링크이며 입장 시 자동으로 만료됩니다. "
                  f"{_INVITE_EXPIRE_HOURS}시간 내 사용해 주세요.\n"
                  f"문의: @batiinvest"
              ))

    sb = _sb()
    if sb:
        try:
            from datetime import datetime, timezone
            sb.table('room_entries').update({
                'status':      'approved',
                'invite_link': link,
                'approved_at': datetime.now(timezone.utc).isoformat(),
                'approved_by': str(admin_id) if admin_id else None,
            }).eq('telegram_id', uid).eq('room_id', room_id) \
              .eq('status', 'pending').execute()
        except Exception as e:
            log.warning(f"[room] approve DB 업데이트 실패: {e}")

    if not res.get('ok'):
        return False, "링크 DM 발송 실패 — 사용자가 봇을 차단했을 수 있습니다."
    log.info(f"[room] 입장 승인: {uid} → {room['name']}")
    return True, f"{room['name']} 입장 승인 완료"


def reject_room(uid: int, room_id) -> bool:
    """거절 → status='rejected' + 신청자에게 안내 DM."""
    sb = _sb()
    if sb:
        try:
            sb.table('room_entries').update({'status': 'rejected'}) \
              .eq('telegram_id', uid).eq('room_id', room_id) \
              .eq('status', 'pending').execute()
        except Exception as e:
            log.debug(f"[room] reject DB 업데이트 실패(무시): {e}")

    _tg('sendMessage', chat_id=uid, parse_mode='HTML',
        text=("입장 신청이 확인되지 않았습니다.\n"
              "결제 내역의 @username과 일치하지 않을 수 있어요. 문의: @batiinvest"))
    log.info(f"[room] 입장 거절: {uid} (room {room_id})")
    return True
