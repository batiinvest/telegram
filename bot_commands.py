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


_menu_setup_done = False

# 입력창 하단에 항상 고정되는 메뉴 (reply keyboard). 첫 /start 이후 계속 노출됨.
_MENU_KEYBOARD = {
    'keyboard': [
        [{'text': '🎟 유료 채팅방 입장'}],
    ],
    'resize_keyboard': True,
    'is_persistent': True,
}

# 관리자 진입 시 보이는 버튼 메뉴 (화이트리스트 관리자만 노출)
_ADMIN_KEYBOARD = {
    'keyboard': [
        [{'text': '🧹 미접속 강퇴'}, {'text': '📋 방 목록'}],
        [{'text': '🚫 아이디 차단'}, {'text': '♻️ 차단 해제'}],
        [{'text': '📊 현황'}, {'text': '🎟 유료 채팅방 입장'}],
    ],
    'resize_keyboard': True,
    'is_persistent': True,
}

_pending_input = {}  # 관리자 id 입력 대기 (chat_id -> 'ban'/'unban')


def _ensure_menu():
    """봇 입력창 옆 ≡ 메뉴에 명령어 등록 (프로세스당 1회).
    사용자가 /start를 직접 치지 않아도 메뉴 버튼이 보이게 합니다."""
    global _menu_setup_done
    if _menu_setup_done:
        return
    _menu_setup_done = True
    try:
        _post('setMyCommands', commands=[
            {'command': 'start', 'description': '🎟 유료 채팅방 입장'},
        ])
        _post('setChatMenuButton', menu_button={'type': 'commands'})
        log.info("[cmd] 봇 메뉴(명령어) 등록 완료")
    except Exception as e:
        log.debug(f"[cmd] 메뉴 등록 실패(무시): {e}")


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

    _ensure_menu()

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

    # 전달(forward)된 메시지 → 원작성자 id 추출 후 차단 안내 (관리자 DM 전용)
    _fwd = msg.get('forward_from') or (msg.get('forward_origin') or {}).get('sender_user')
    if isinstance(_fwd, dict) and _fwd.get('id'):
        import room_access as _ra
        if not _ra.is_room_admin(uid):
            return
        import html as _h
        _pending_input.pop(chat_id, None)
        _tid = _fwd['id']
        _nm = (_fwd.get('first_name') or '')
        if _fwd.get('last_name'):
            _nm += ' ' + _fwd['last_name']
        _un = _fwd.get('username')
        _info = '🔎 전달된 메시지 작성자' + chr(10) + '<b>' + _h.escape(_nm.strip() or str(_tid)) + '</b>'
        if _un:
            _info += ' (@' + _h.escape(_un) + ')'
        _info += chr(10) + 'id <code>' + str(_tid) + '</code>'
        _post('sendMessage', chat_id=chat_id, parse_mode='HTML', text=_info,
              reply_markup={'inline_keyboard': [[
                  {'text': '🚫 전체 방 차단', 'callback_data': 'SPAM|banall|' + str(_tid)},
                  {'text': '♻️ 차단 해제', 'callback_data': 'SPAM|unban|' + str(_tid)}]]})
        return
    # 전달했지만 원작성자가 계정 링크를 숨긴 경우 (id 획득 불가)
    if msg.get('forward_sender_name') or (msg.get('forward_origin') or {}).get('type') == 'hidden_user':
        import room_access as _ra
        if not _ra.is_room_admin(uid):
            return
        _pending_input.pop(chat_id, None)
        _post('sendMessage', chat_id=chat_id, parse_mode='HTML',
              text='⚠️ 이 사용자는 전달 시 계정을 숨겨서 id를 알 수 없습니다.' + chr(10)
                   + '그룹에서 그 사람 메시지에 <b>답장</b>하며 <code>/차단</code> 을 보내면 확실히 차단됩니다.')
        return

    cmd = ''
    payload = ''
    if text.startswith('/'):
        _bits = text.split()
        cmd = _bits[0].split('@')[0].lower()
        payload = _bits[1] if len(_bits) > 1 else ''

    # 관리자 id 입력 대기 (아이디 차단/해제)
    if chat_id in _pending_input:
        _act = _pending_input.get(chat_id)
        _btns = ('🧹 미접속 강퇴', '📋 방 목록', '🚫 아이디 차단', '♻️ 차단 해제', '🎟 유료 채팅방 입장')
        if text in _btns or text.startswith('/'):
            _pending_input.pop(chat_id, None)
            if text == '/취소':
                _reply(chat_id, '취소되었습니다.')
                return
        else:
            _pending_input.pop(chat_id, None)
            if _act == 'ban':
                _run_ban(chat_id, uid, text)
            else:
                _run_unban(chat_id, uid, text)
            return

    # ── 하단 고정 메뉴(reply keyboard) 버튼 처리 ──
    if text == '🎟 유료 채팅방 입장':
        try:
            import room_access as _ra
            _ra.start_entry(uid, username=username,
                            name=f"{fname} {lname}".strip())
        except Exception as e:
            log.error(f"[cmd] 메뉴 입장 오류: {e}")
            _reply(chat_id, "처리 중 오류가 발생했습니다. 문의: @batiinvest")
        return
    if text == '🧹 미접속 강퇴':
        _run_inactive_menu(chat_id, uid)
        return
    if text == '📋 방 목록':
        _run_room_list(chat_id, uid)
        return
    if text == '📊 현황':
        _run_status(chat_id, uid)
        return
    if text == '🚫 아이디 차단':
        import room_access as _ra
        if not _ra.is_room_admin(uid):
            _reply(chat_id, "⛔ 관리자 전용 기능입니다.")
            return
        _pending_input[chat_id] = 'ban'
        _reply(chat_id, '🚫 차단할 텔레그램 <b>id</b>(또는 @아이디)를 입력하세요.' + chr(10) + '(취소: /취소)')
        return
    if text == '♻️ 차단 해제':
        import room_access as _ra
        if not _ra.is_room_admin(uid):
            _reply(chat_id, "⛔ 관리자 전용 기능입니다.")
            return
        _pending_input[chat_id] = 'unban'
        _reply(chat_id, '♻️ 차단 해제할 텔레그램 <b>id</b>(또는 @아이디)를 입력하세요.' + chr(10) + '(취소: /취소)')
        return
    # ── 관리자: 진입 시 관리 버튼 메뉴 ──
    if cmd == '/start' or not cmd:
        try:
            import room_access as _ra0
            if _ra0.is_room_admin(uid):
                _post('sendMessage', chat_id=uid, parse_mode='HTML',
                      text='🛠 <b>바티 관리자 메뉴</b>' + chr(10) + '아래 버튼으로 관리하세요 👇',
                      reply_markup=_ADMIN_KEYBOARD)
                return
        except Exception:
            pass
    # ── /start (페이로드 무관) · 일반 메시지 → 고정 메뉴 노출 + 방 선택 ──
    if cmd == '/start' or not cmd:
        try:
            _post('sendMessage', chat_id=uid, parse_mode='HTML',
                  text=("🎟 <b>바티인베스트 유료 채팅방</b>\n"
                        "아래 메뉴는 항상 여기 있어요 👇"),
                  reply_markup=_MENU_KEYBOARD)
            import room_access as _ra
            _ra.start_entry(uid, username=username,
                            name=f"{fname} {lname}".strip())
        except Exception as e:
            log.error(f"[cmd] start 오류: {e}")
            _reply(chat_id, "처리 중 오류가 발생했습니다. 문의: @batiinvest")
        return

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

    # ── 관리자 채팅방 관리 (방목록 / 방상태 / 방연결) ──────────
    elif cmd in ('/방목록', '/rooms', '/방상태', '/방연결'):
        try:
            import room_access as _ra
            if not _ra.is_room_admin(uid):
                _reply(chat_id, "⛔ 관리자 전용 명령입니다.")
                return
            args = text.split()[1:]
            if cmd in ('/방목록', '/rooms'):
                _run_room_list(chat_id, uid)
            elif cmd == '/방상태':
                if len(args) < 2:
                    _reply(chat_id, "사용법: <code>/방상태 [종목명|id] [open|paid|full]</code>")
                    return
                st = args[-1].lower()
                target = ' '.join(args[:-1])
                if st not in _ra.ALL_STATUSES:
                    _reply(chat_id, "상태는 open / paid / full 중 하나여야 합니다.")
                    return
                r = _ra.find_room(target)
                if not r:
                    _reply(chat_id, f"'{target}' 방을 찾을 수 없습니다.")
                    return
                if r.get('_multi'):
                    _reply(chat_id, "여러 방이 일치합니다. 더 정확히:\n• " + "\n• ".join(r['_multi']))
                    return
                ok = _ra.set_room_status(r['id'], st)
                _reply(chat_id, f"✅ <b>{r['name']}</b> 상태 → <b>{st}</b>" if ok else "⚠️ 변경 실패")
            elif cmd == '/방연결':
                if len(args) < 2:
                    _reply(chat_id, "사용법: <code>/방연결 [종목명|id] [chat_id]</code>")
                    return
                new_cid = args[-1]
                target = ' '.join(args[:-1])
                r = _ra.find_room(target)
                if not r:
                    _reply(chat_id, f"'{target}' 방을 찾을 수 없습니다.")
                    return
                if r.get('_multi'):
                    _reply(chat_id, "여러 방이 일치합니다. 더 정확히:\n• " + "\n• ".join(r['_multi']))
                    return
                ok = _ra.set_room_chat_id(r['id'], new_cid)
                _reply(chat_id, f"✅ <b>{r['name']}</b> chat_id → <code>{new_cid}</code>" if ok else "⚠️ 변경 실패")
        except Exception as e:
            log.error(f"[cmd] 방관리 오류: {e}")
            _reply(chat_id, "처리 중 오류가 발생했습니다.")

    # ── 미접속 강퇴 메뉴 (관리자) ──────────────────────────────
    elif cmd in ('/미접속', '/강퇴'):
        _run_inactive_menu(chat_id, uid)

    # ── 아이디 차단 / 차단해제 (관리자) ──────────────────────
    elif cmd in ('/차단', '/ban', '/차단해제', '/unban'):
        _unban = cmd in ('/차단해제', '/unban')
        if not payload:
            _reply(chat_id, ('사용법: <code>/차단해제 [id 또는 @아이디]</code>' if _unban
                             else '사용법: <code>/차단 [id 또는 @아이디]</code>'))
        else:
            (_run_unban if _unban else _run_ban)(chat_id, uid, payload)

    # ── 관리자 현황 요약 ──────────────────────────────────────
    elif cmd in ('/현황', '/status2'):
        _run_status(chat_id, uid)


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


def _reply_long(chat_id: int, text: str):
    """4096자 제한 대응 — 줄 단위로 잘라 여러 메시지로 발송."""
    LIMIT = 3800
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > LIMIT:
            if buf:
                _reply(chat_id, buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        _reply(chat_id, buf)


def _fmt_room_list(rooms: list) -> str:
    """관리자용 방 목록 텍스트."""
    import html as _h
    if not rooms:
        return "등록된 방이 없습니다."
    _ICON = {'paid': '🔒', 'full': '🚫', 'open': '🟢'}
    lines = [f"📋 <b>채팅방 목록</b> ({len(rooms)}개)"]
    cur = None
    for r in rooms:
        cat = r.get('cat') or '기타'
        if cat != cur:
            cur = cat
            lines.append(f"\n<b>[{_h.escape(str(cat))}]</b>")
        icon = _ICON.get(r.get('status'), '·')
        nm = _h.escape(str(r.get('name') or ''))
        cid = r.get('chat_id') or '—'
        flag = '✅' if str(cid).lstrip('-').isdigit() else '⚠️'
        mem = r.get('members') or 0
        mx = r.get('max_members') or 1000
        lines.append(f"{icon} <b>{nm}</b> <code>#{r.get('id')}</code> {mem}/{mx} {flag}<code>{_h.escape(str(cid))}</code>")
    lines.append("\n명령: /방상태 [종목] [open|paid|full] · /방연결 [종목] [chat_id]")
    return "\n".join(lines)


def _build_room_list(page=0):
    import room_access as _ra
    _NL = chr(10)
    rooms = _ra.list_rooms()
    PER = 8
    pages = max(1, (len(rooms) + PER - 1) // PER)
    page = max(0, min(page, pages - 1))
    chunk = rooms[page * PER:(page + 1) * PER]
    _IC = {'paid': '🔒', 'full': '🚫', 'open': '🟢'}
    kb = []
    for r in chunk:
        ic = _IC.get(r.get('status'), '·')
        flag = '' if str(r.get('chat_id') or '').lstrip('-').isdigit() else '⚠️'
        kb.append([{'text': ic + flag + ' ' + r['name'] + ' (' + str(r.get('members') or 0) + ')',
                    'callback_data': 'RM|room|' + str(r['id']) + '|' + str(page)}])
    nav = []
    if page > 0:
        nav.append({'text': '◀ 이전', 'callback_data': 'RM|pg|' + str(page - 1)})
    nav.append({'text': str(page + 1) + '/' + str(pages), 'callback_data': 'RM|nop'})
    if page < pages - 1:
        nav.append({'text': '다음 ▶', 'callback_data': 'RM|pg|' + str(page + 1)})
    kb.append(nav)
    text = ('📋 <b>채팅방 관리</b> (' + str(len(rooms)) + '개) · ' + str(page + 1) + '/' + str(pages) + 'p' + _NL
            + '방을 선택해 상태를 변경하세요. ⚠️=chat_id 미연결')
    return text, {'inline_keyboard': kb}


def _build_room_detail(rid, page=0):
    import room_access as _ra
    import html as _h
    _NL = chr(10)
    r = _ra.room_detail(rid)
    if not r:
        return '방을 찾을 수 없습니다.', {'inline_keyboard': [[{'text': '◀ 목록', 'callback_data': 'RM|pg|' + str(page)}]]}
    _IC = {'paid': '🔒', 'full': '🚫', 'open': '🟢'}
    cid = r.get('chat_id') or '—'
    if str(cid).lstrip('-').isdigit():
        ok = '✅'
    else:
        ok = '⚠️ 미연결 (봇을 그룹 관리자로 추가하면 자동 등록)'
    st = r.get('status')
    text = ('🏠 <b>' + _h.escape(r['name']) + '</b>' + _NL
            + '상태: ' + _IC.get(st, '') + ' <b>' + str(st) + '</b>' + _NL
            + '정원: ' + str(r.get('members') or 0) + '/' + str(r.get('max_members') or 1000) + _NL
            + 'chat_id: <code>' + _h.escape(str(cid)) + '</code> ' + ok)
    def _b(label, val):
        mark = '● ' if st == val else ''
        return {'text': mark + label, 'callback_data': 'RM|st|' + str(rid) + '|' + val + '|' + str(page)}
    kb = [[_b('🟢 일반', 'open'), _b('🔒 유료', 'paid'), _b('🚫 마감', 'full')],
          [{'text': '◀ 목록', 'callback_data': 'RM|pg|' + str(page)}]]
    return text, {'inline_keyboard': kb}


def _run_room_list(chat_id, uid):
    import room_access as _ra
    if not _ra.is_room_admin(uid):
        _reply(chat_id, "⛔ 관리자 전용 기능입니다.")
        return
    text, kb = _build_room_list(0)
    _post('sendMessage', chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=kb)


def _send_inactive_menu(chat_id, rooms, total, age_min):
    _NL = chr(10)
    import inactive_kick as _ik
    _d = str(_ik.INACTIVE_DAYS)
    items = sorted([(rid, i) for rid, i in rooms.items() if i['cands']],
                   key=lambda x: -len(x[1]['cands']))
    if not items:
        _post('sendMessage', chat_id=chat_id, text="✅ 미접속(" + _d + "일+) 대상자가 없습니다.")
        return
    btns = []
    for rid, info in items:
        lock = '🔒' if info['status'] == 'paid' else ''
        btns.append({'text': lock + info['name'] + ' (' + str(len(info['cands'])) + ')',
                     'callback_data': 'IK|sel|' + str(rid)})
    kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    kb.append([{'text': '⚠️ 전체 강퇴 (' + str(total) + ')', 'callback_data': 'IK|sel|all'}])
    kb.append([{'text': '🔄 다시 스캔', 'callback_data': 'IK|rescan'}])
    age_txt = '방금 스캔' if age_min is None else ('🕒 ' + str(int(age_min)) + '분 전')
    head = ('🧹 <b>미접속(' + _d + '일+) 강퇴</b> · 대상 ' + str(total) + '명 / '
            + str(len(items)) + '개 방 · ' + age_txt + _NL
            + '방 선택 → 명단 확인 후 강퇴 (강퇴해도 이 메뉴는 유지됩니다)')
    _post('sendMessage', chat_id=chat_id, text=head, parse_mode='HTML',
          reply_markup={'inline_keyboard': kb})


def _run_inactive_menu(chat_id, uid, force=False):
    import room_access as _ra
    if not _ra.is_room_admin(uid):
        _reply(chat_id, "⛔ 관리자 전용 기능입니다.")
        return
    import threading
    import inactive_kick as _ik
    if not force and _ik.cache_fresh():
        rooms, total, _ts = _ik.get_cache()
        _send_inactive_menu(chat_id, rooms, total, _ik.cache_age_min())
        return
    _reply(chat_id, "🔎 미접속 멤버 스캔 중입니다... (54개 방, 약 3분 소요)")
    def _scan_worker(_cid):
        try:
            rooms, total = _ik.scan_candidates()
            _send_inactive_menu(_cid, rooms, total, None)
        except Exception as _se:
            log.error(f"[cmd] 미접속 스캔 오류: {_se}")
            _post('sendMessage', chat_id=_cid, text="스캔 중 오류가 발생했습니다.")
    threading.Thread(target=_scan_worker, args=(chat_id,), daemon=True).start()


def _run_ban(chat_id, uid, query):
    import room_access as _ra
    if not _ra.is_room_admin(uid):
        _reply(chat_id, "⛔ 관리자 전용 기능입니다.")
        return
    import threading, spam_guard as _sg, html as _h
    def _w():
        q = str(query).strip()
        # 숫자 id 또는 @아이디 → 기존 경로(직접 차단)
        if q.lstrip('-').isdigit() or q.startswith('@'):
            target, disp = _sg.resolve_user(q)
            if not target:
                _post('sendMessage', chat_id=chat_id, text='❌ ' + str(disp))
                return
            n = _sg.ban_all(target)
            _post('sendMessage', chat_id=chat_id, parse_mode='HTML',
                  text='🚫 <b>' + _h.escape(str(disp)) + '</b> (id <code>' + str(target) + '</code>) — ' + str(n) + '개 방에서 차단',
                  reply_markup={'inline_keyboard': [[{'text': '♻️ 차단 해제', 'callback_data': 'SPAM|unban|' + str(target)}]]})
            return
        # 표시이름(닉네임) → Telethon 멤버 검색 후 후보 버튼
        try:
            import inactive_kick as _ik
            cands = _ik.search_members_by_name(q)
        except Exception as e:
            log.error('[cmd] 이름검색 오류: ' + str(e))
            _post('sendMessage', chat_id=chat_id,
                  text='⚠️ 이름 검색 중 오류가 발생했습니다. 숫자 id 또는 @아이디로 시도하세요.')
            return
        if not cands:
            _post('sendMessage', chat_id=chat_id, parse_mode='HTML',
                  text="❌ '" + _h.escape(q) + "' 이름의 멤버를 찾지 못했습니다." + chr(10)
                       + "숫자 id 또는 @아이디로 시도하세요.")
            return
        kb = []
        for c in cands:
            label = c['name'] + ((' ' + c['username']) if c['username'] else '') \
                    + ' · ' + str(len(c['rooms'])) + '개방'
            kb.append([{'text': label, 'callback_data': 'SPAM|banall|' + str(c['id'])}])
        note = '🔎 <b>' + _h.escape(q) + '</b> 검색 결과 — 차단할 사람을 누르세요.'
        if len(cands) >= 8:
            note += chr(10) + '(결과가 많음 — 이름을 더 정확히/‘@아이디’로 좁히세요)'
        _post('sendMessage', chat_id=chat_id, parse_mode='HTML', text=note,
              reply_markup={'inline_keyboard': kb})
    threading.Thread(target=_w, daemon=True).start()
    _reply(chat_id, "🚫 차단 처리 중…")


def _run_unban(chat_id, uid, query):
    import room_access as _ra
    if not _ra.is_room_admin(uid):
        _reply(chat_id, "⛔ 관리자 전용 기능입니다.")
        return
    import threading, spam_guard as _sg, html as _h
    def _w():
        target, disp = _sg.resolve_user(query)
        if not target:
            _post('sendMessage', chat_id=chat_id, text='❌ ' + str(disp))
            return
        n = _sg.unban_all(target)
        _post('sendMessage', chat_id=chat_id, parse_mode='HTML',
              text='♻️ <b>' + _h.escape(str(disp)) + '</b> (id <code>' + str(target) + '</code>) — ' + str(n) + '개 방에서 차단 해제',
              reply_markup={'inline_keyboard': [[{'text': '🚫 다시 차단', 'callback_data': 'SPAM|banall|' + str(target)}]]})
    threading.Thread(target=_w, daemon=True).start()
    _reply(chat_id, "♻️ 차단 해제 처리 중…")


def _run_status(chat_id, uid):
    import room_access as _ra
    if not _ra.is_room_admin(uid):
        _reply(chat_id, "⛔ 관리자 전용 기능입니다.")
        return
    s = _ra.entry_stats()
    _NL = chr(10)
    lines = ['📊 <b>유료방 입장 현황</b>',
             '방 ' + str(s['rooms']) + '개 (유료 ' + str(s['paid_rooms']) + ')',
             '─────',
             '⏳ 대기(pending): ' + str(s['pending']) + '명',
             '✅ 오늘 승인: ' + str(s['today_approved']) + '명',
             '🚪 입장 완료: ' + str(s['joined']) + '명',
             '⚠️ 미입장(승인후 안들어옴): ' + str(s['approved_not_joined']) + '명']
    if s['not_joined_rooms']:
        lines.append('─ 미입장 방별:')
        for rn, c in sorted(s['not_joined_rooms'].items(), key=lambda x: -x[1]):
            lines.append('  · ' + rn + ': ' + str(c))
    _reply(chat_id, _NL.join(lines))
