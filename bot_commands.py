"""
bot_commands.py — 텔레그램 봇 명령어 수신 처리
───────────────────────────────────────────────
getUpdates long-polling으로 사용자 메시지를 수신하고
간단한 명령어에 응답합니다. run_all.py에서 데몬 스레드로 기동.

지원 명령어:
  /myid   — 내 텔레그램 숫자 ID 확인 (프로 채널 구독 시 필요)
  /start  — 봇 소개 메시지
  /status — 구독 만료일 조회 (pro_members 등록된 경우)
"""

import logging
import time
import requests
import threading

log = logging.getLogger(__name__)

try:
    from config import TELEGRAM_BOT_TOKEN as _TOKEN
except ImportError:
    _TOKEN = None

try:
    import pro_channel as _pro
    _PRO_OK = True
except ImportError:
    _PRO_OK = False

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except ImportError:
    _BRIDGE_OK = False

from telegram_utils import get_admin_chat_id as _get_admin_chat

_TG = "https://api.telegram.org/bot{token}/{method}"
_running = False


def _post(method: str, **params) -> dict:
    if not _TOKEN:
        return {}
    try:
        r = requests.post(
            _TG.format(token=_TOKEN, method=method),
            json=params, timeout=15
        )
        return r.json()
    except Exception as e:
        log.debug(f"[cmd] {method} 오류: {e}")
        return {}


def _reply(chat_id: int, text: str):
    _post('sendMessage', chat_id=chat_id, text=text, parse_mode='HTML')



def _notify_admin_subscribe(uid: int, fname: str, lname: str, username: str):
    """구독 신청 내용을 어드민에게 전달 (인라인 버튼 포함)."""
    admin = _get_admin_chat()
    if not admin:
        log.warning(f"[cmd] 어드민 chat_id 미설정 — 구독 신청 알림 전송 불가 (uid={uid})")
        return

    name_display  = f"{fname} {lname}".strip()
    uname_display = f"@{username}" if username else "없음"
    msg = (
        f"📩 <b>[프로 채널 구독 신청]</b>\n\n"
        f"이름: <b>{name_display}</b>\n"
        f"@username: {uname_display}\n"
        f"텔레그램 ID: <code>{uid}</code>"
    )
    # 인라인 버튼: 1개월 승인 / 3개월 승인 / 거절
    keyboard = {'inline_keyboard': [[
        {'text': '✅ 1개월 승인 + 초대', 'callback_data': f'PRO|approve|{uid}|1'},
        {'text': '✅ 3개월',             'callback_data': f'PRO|approve|{uid}|3'},
        {'text': '❌ 거절',              'callback_data': f'PRO|reject|{uid}'},
    ]]}
    res = _post('sendMessage', chat_id=admin, parse_mode='HTML',
                text=msg, reply_markup=keyboard)
    if res.get('ok'):
        log.info(f"[cmd] 어드민 알림 전송 완료 → {admin}")
    else:
        log.warning(f"[cmd] 어드민 알림 실패: {res.get('description')} (admin={admin})")


def _handle(update: dict):
    """단일 update 처리."""
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return

    # 봇과의 1:1 DM만 처리 (그룹 메시지 무시)
    if msg['chat']['type'] != 'private':
        return

    chat_id  = msg['chat']['id']
    user     = msg.get('from', {})
    uid      = user.get('id', chat_id)
    fname    = user.get('first_name', '')
    lname    = user.get('last_name', '')
    username = user.get('username', '')
    text     = (msg.get('text') or '').strip()

    cmd = ''
    if text.startswith('/'):
        cmd = text.split()[0].split('@')[0].lower()

    # ── /start 또는 일반 메시지 ───────────────────────────────
    if cmd == '/start' or not cmd:
        # 이미 구독 중인지 확인
        if _PRO_OK:
            try:
                member = _pro.get_member(uid)
                if member and member.get('is_active'):
                    from datetime import date
                    until = member['paid_until']
                    days  = (date.fromisoformat(until) - date.today()).days
                    _reply(chat_id,
                        f"안녕하세요, {fname}님! 👋\n\n"
                        f"✅ 구독 중 — 만료일 <b>{until}</b> (D-{days})\n\n"
                        f"/status — 구독 현황 상세 조회"
                    )
                    return
            except Exception:
                pass

        # 미구독자: 구독 안내 + 자동으로 어드민 알림
        _reply(chat_id,
            f"안녕하세요, {fname}님! 👋\n\n"
            f"<b>바티인베스트 증권사 리포트 채널</b>에 관심 가져주셔서 감사합니다.\n\n"
            f"구독 신청이 접수되었습니다.\n"
            f"담당자가 확인 후 구독 안내를 드릴게요.\n\n"
            f"문의: @batiinvest"
        )
        # 어드민에게 신청자 정보 전달
        _notify_admin_subscribe(uid, fname, lname, username)
        log.info(f"[cmd] 구독 신청: {uid} ({fname} {lname} @{username})")

    # ── /status ──────────────────────────────────────────────
    elif cmd == '/status':
        if not _PRO_OK:
            _reply(chat_id, "서비스를 일시적으로 이용할 수 없습니다.")
            return
        try:
            member = _pro.get_member(uid)
            if not member:
                _reply(chat_id,
                    "❌ 등록된 구독 정보가 없습니다.\n\n"
                    "구독 문의: @batiinvest"
                )
            else:
                from datetime import date
                until = member['paid_until']
                in_ch = member.get('in_channel', False)
                days  = (date.fromisoformat(until) - date.today()).days

                if days < 0:
                    status = "⛔ 만료됨"
                elif days == 0:
                    status = "⚠️ 오늘 만료"
                elif days <= 7:
                    status = f"⏰ D-{days} (곧 만료)"
                else:
                    status = f"✅ 구독 중 (D-{days})"

                _reply(chat_id,
                    f"📋 <b>구독 현황</b>\n\n"
                    f"상태: {status}\n"
                    f"만료일: <b>{until}</b>\n"
                    f"채널 입장: {'✅' if in_ch else '❌'}\n\n"
                    f"{'갱신 문의: @batiinvest' if days < 7 else ''}"
                )
        except Exception as e:
            log.error(f"[cmd] /status 오류: {e}")
            _reply(chat_id, "조회 중 오류가 발생했습니다.")

    # ── /myid (레거시 지원) ───────────────────────────────────
    elif cmd == '/myid':
        _reply(chat_id,
            f"🔢 내 텔레그램 ID: <code>{uid}</code>"
        )


def run_polling():
    """
    Telegram getUpdates long-polling 루프.
    봇이 받은 메시지를 처리합니다. (데몬 스레드로 실행)
    """
    global _running
    if not _TOKEN:
        log.warning("[cmd] BOT_TOKEN 없음 — 명령어 수신 비활성화")
        return

    _running  = True
    offset    = 0
    log.info("🤖 [봇 명령어] 수신 시작 (/myid, /status, /start)")

    while _running:
        try:
            data = _post('getUpdates',
                         offset=offset,
                         timeout=30,
                         allowed_updates=['message'])

            if not data.get('ok'):
                time.sleep(5)
                continue

            for update in data.get('result', []):
                offset = update['update_id'] + 1
                try:
                    _handle(update)
                except Exception as e:
                    log.error(f"[cmd] handle 오류: {e}")

        except Exception as e:
            log.error(f"[cmd] polling 오류: {e}")
            time.sleep(10)


def start_thread() -> threading.Thread:
    """데몬 스레드로 polling 시작."""
    t = threading.Thread(target=run_polling,
                         name="Thread-BotCmd",
                         daemon=True)
    t.start()
    return t


def stop():
    global _running
    _running = False
