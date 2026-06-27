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
import html as _html
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


def _esc(s) -> str:
    """텔레그램 HTML parse_mode용 이스케이프 (&, <, >). 사용자/방 이름에 적용."""
    return _html.escape(str(s or ''), quote=False)


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

    rname = _esc(room['name'])

    # 이미 승인됐거나 대기 중이면 중복 신청 차단 (pending 중복 → 이중승인·unique충돌 방지)
    try:
        dup = sb.table('room_entries').select('id,status') \
                .eq('telegram_id', uid).eq('room_id', room_id) \
                .in_('status', ['approved', 'pending']).execute()
        rows = dup.data or []
        if any(r['status'] == 'approved' for r in rows):
            _tg('sendMessage', chat_id=uid, parse_mode='HTML',
                text=(f"이미 <b>{rname}</b> 입장이 승인된 계정입니다.\n"
                      f"링크 분실 시 @batiinvest로 문의해 주세요."))
            return False
        if any(r['status'] == 'pending' for r in rows):
            _tg('sendMessage', chat_id=uid, parse_mode='HTML',
                text=(f"<b>{rname}</b> 입장 신청이 이미 접수되어 확인 중입니다.\n"
                      f"조금만 기다려 주세요. 문의: @batiinvest"))
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
        text=(f"✅ <b>{rname}</b> 입장 신청이 접수되었습니다.\n\n"
              f"후원 확인 후 <b>1회용 입장 링크</b>를 보내드립니다. 잠시만 기다려 주세요.\n\n"
              f"※ 아직 후원 전이라면 litt.ly/batiinvest 에서 후원해 주세요.\n"
              f"※ 후원 시 응원 메시지(비밀)에 <b>텔레그램 @아이디</b>(없으면 <b>텔레그램 이름</b>)를 적어주세요.\n"
              f"※ 후원 내역과 대조가 안 되면 입장이 거절될 수 있습니다."))

    _notify_admin(uid, username, name, room)
    log.info(f"[room] 입장 신청: {uid}(@{username}) → {room['name']}")
    return True


def _notify_admin(uid: int, username: str, name: str, room: dict):
    """어드민에게 승인/거절 버튼 알림 발송."""
    admin = _get_admin_chat()
    if not admin:
        log.warning("[room] admin_chat_id 미설정 — 입장 신청 알림 불가")
        return
    uname = ('@' + _esc(username)) if username else "없음"
    msg = (
        f"🎟 <b>[유료방 입장 신청]</b>\n\n"
        f"방: <b>{_esc(room['name'])}</b>\n"
        f"이름: {_esc(name) or '-'}\n"
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

    sb = _sb()

    # 이미 승인된 사용자면 새 링크 발급 안 함 (이중 승인·링크 중복 방지)
    if sb:
        try:
            done = sb.table('room_entries').select('id') \
                     .eq('telegram_id', uid).eq('room_id', room_id) \
                     .eq('status', 'approved').execute()
            if done.data:
                return False, "이미 승인된 사용자입니다 (링크 기발급)."
        except Exception as e:
            log.debug(f"[room] approve 중복 확인 오류(무시): {e}")

    link = create_invite_link(room['chat_id'])
    if not link:
        return False, "초대 링크 생성 실패 — 봇이 해당 방 관리자인지 확인하세요."

    res = _tg('sendMessage', chat_id=uid, parse_mode='HTML',
              text=(
                  f"🔓 <b>{_esc(room['name'])}</b> 입장 링크입니다.\n\n"
                  f"🔗 {link}\n\n"
                  f"⚠️ 1회용 링크이며 입장 시 자동으로 만료됩니다. "
                  f"{_INVITE_EXPIRE_HOURS}시간 내 사용해 주세요.\n"
                  f"문의: @batiinvest"
              ))

    # DM 성공했을 때만 'approved' 마킹 (실패 시 pending 유지 → 재승인 가능)
    if not res.get('ok'):
        return False, "링크 DM 발송 실패 — 사용자가 봇을 차단했을 수 있습니다."

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
        text=("❌ 입장 신청이 거절되었습니다.\n\n"
              "후원 내역이 없거나, 후원 시 적으신 @아이디(또는 텔레그램 이름)가 일치하지 않습니다.\n"
              "litt.ly/batiinvest 에서 후원 후, 응원 메시지(비밀)에 텔레그램 @아이디"
              "(없으면 텔레그램 이름)를 정확히 적어 다시 신청해 주세요.\n\n"
              "문의: @batiinvest"))
    log.info(f"[room] 입장 거절: {uid} (room {room_id})")
    return True


# ══════════════════════════════════════════════════════════════
# 🛠️  관리자 채팅방 관리 (봇 명령 + chat_id 자동등록)  [2026-06-27]
# ══════════════════════════════════════════════════════════════

ALL_STATUSES = ('open', 'paid', 'full')


def is_room_admin(uid) -> bool:
    """uid가 관리자 그룹(pro_admin_chat_id, 숫자ID)의 멤버인지 확인."""
    admin = _get_admin_chat()
    if not admin or not str(admin).lstrip('-').isdigit():
        return False
    res = _tg('getChatMember', chat_id=admin, user_id=uid)
    if not res.get('ok'):
        return False
    return res.get('result', {}).get('status') in ('creator', 'administrator', 'member')


def list_rooms() -> list:
    """전체 방 목록 (관리용)."""
    sb = _sb()
    if not sb:
        return []
    try:
        res = sb.table('rooms') \
                .select('id,name,chat_id,status,members,max_members,cat,room_type') \
                .order('cat').order('name').execute()
        return res.data or []
    except Exception as e:
        log.error(f"[room] 목록 조회 실패: {e}")
        return []


def find_room(query) -> Optional[dict]:
    """id(숫자) 또는 종목명(부분일치)으로 방 검색. 다중일치 시 {'_multi':[names]}."""
    sb = _sb()
    if not sb:
        return None
    q = str(query).strip()
    try:
        if q.isdigit():
            r = sb.table('rooms').select('id,name,chat_id,status').eq('id', int(q)).execute()
            if r.data:
                return r.data[0]
        r = sb.table('rooms').select('id,name,chat_id,status').ilike('name', f'%{q}%').execute()
        data = r.data or []
        if len(data) == 1:
            return data[0]
        if len(data) > 1:
            exact = [d for d in data if d['name'] == q]
            if len(exact) == 1:
                return exact[0]
            return {'_multi': [d['name'] for d in data]}
        return None
    except Exception as e:
        log.error(f"[room] 검색 실패: {e}")
        return None


def set_room_status(room_id, status) -> bool:
    if status not in ALL_STATUSES:
        return False
    sb = _sb()
    if not sb:
        return False
    try:
        sb.table('rooms').update({'status': status}).eq('id', room_id).execute()
        return True
    except Exception as e:
        log.error(f"[room] 상태변경 실패: {e}")
        return False


def set_room_chat_id(room_id, chat_id) -> bool:
    sb = _sb()
    if not sb:
        return False
    try:
        sb.table('rooms').update({'chat_id': str(chat_id)}).eq('id', room_id).execute()
        return True
    except Exception as e:
        log.error(f"[room] chat_id 설정 실패: {e}")
        return False


def handle_bot_added_to_group(chat_id, title) -> None:
    """봇이 그룹 관리자로 추가될 때: 종목명 매칭 자동 chat_id 등록 + 관리자 알림."""
    admin = _get_admin_chat()
    rooms = list_rooms()
    t = title or ''
    matches = [r for r in rooms if r.get('name') and r['name'] in t]
    if len(matches) == 1:
        r = matches[0]
        old = r.get('chat_id')
        if str(old) == str(chat_id):
            return
        set_room_chat_id(r['id'], chat_id)
        if admin:
            _tg('sendMessage', chat_id=admin, parse_mode='HTML',
                text=(f"🔗 <b>chat_id 자동 등록</b>\n\n"
                      f"방: <b>{_esc(r['name'])}</b> (id={r['id']})\n"
                      f"그룹: {_esc(title)}\n"
                      f"chat_id: <code>{_esc(old)}</code> → <code>{chat_id}</code>"))
        log.info(f"[room] chat_id 자동등록: {r['name']} → {chat_id}")
    else:
        if admin:
            hint = "여러 방 매칭됨 — 수동 연결 필요" if len(matches) > 1 else "매칭되는 방 없음"
            _tg('sendMessage', chat_id=admin, parse_mode='HTML',
                text=(f"🆕 <b>봇이 그룹 관리자로 추가됨</b> ({hint})\n\n"
                      f"그룹: {_esc(title)}\n"
                      f"chat_id: <code>{chat_id}</code>\n\n"
                      f"수동 연결: <code>/방연결 &lt;종목&gt; {chat_id}</code>"))
        log.info(f"[room] 그룹 추가 감지(미매칭): {title} {chat_id}")
