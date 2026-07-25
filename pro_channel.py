"""
pro_channel.py — 프로 채널 유료 멤버 관리
──────────────────────────────────────────
Supabase `pro_members` 테이블 + Telegram Bot API를 이용해
월정액 구독자의 채널 입장/퇴장을 자동 관리합니다.

주요 기능:
  - add_member()      : 신규 멤버 등록 (DB 저장)
  - extend_member()   : 구독 기간 연장
  - send_invite()     : 1회용 초대 링크 생성 → 멤버 DM 발송
  - kick_member()     : 채널에서 강제 퇴장
  - check_expired()   : 만료 멤버 자동 퇴장 (매일 자동 실행)
  - get_members()     : 멤버 목록 조회
  - sync_channel_status() : 채널 실제 멤버 상태 DB 동기화

환경변수 / Supabase app_config:
  pro_channel_id  : 프로 채널 @username 또는 숫자 ID
  (BOT_TOKEN은 config.py의 TELEGRAM_BOT_TOKEN 사용)
"""

from logger_config import get_logger
log = get_logger(__name__)

import time
import requests
from datetime import date, timedelta
from typing import Optional


# ── 의존 임포트 ──────────────────────────────────────────────
try:
    from config import TELEGRAM_BOT_TOKEN as _BOT_TOKEN
except ImportError:
    _BOT_TOKEN = None

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except ImportError:
    _BRIDGE_OK = False
    log.warning("supabase_bridge 없음 — pro_channel 기능 비활성화")

from telegram_utils import get_admin_chat_id as _get_admin_chat

# ── 상수 ─────────────────────────────────────────────────────
_TG_API = "https://api.telegram.org/bot{token}/{method}"
_DEFAULT_PRO_CHANNEL = "@batipro"   # 기본값 (app_config로 덮어씀)
_EXPIRE_NOTIFY_DAYS  = 3            # 만료 N일 전 사전 알림


# ══════════════════════════════════════════════════════════════
# 🛠️  내부 헬퍼
# ══════════════════════════════════════════════════════════════

def _bot_token() -> Optional[str]:
    """봇 토큰 반환 (환경변수 우선)"""
    return _BOT_TOKEN or None


def _pro_channel_id() -> str:
    """프로 채널 ID — app_config > 기본값"""
    if not _BRIDGE_OK:
        return _DEFAULT_PRO_CHANNEL
    try:
        sb = _bridge._get_client()
        res = sb.table('app_config').select('value') \
                .eq('key', 'pro_channel_id').single().execute()
        return (res.data or {}).get('value') or _DEFAULT_PRO_CHANNEL
    except Exception:
        return _DEFAULT_PRO_CHANNEL


def _tg(method: str, **params) -> dict:
    """Telegram Bot API 호출. 실패 시 {} 반환."""
    token = _bot_token()
    if not token:
        log.error("[pro_channel] BOT_TOKEN 없음")
        return {}
    url = _TG_API.format(token=token, method=method)
    try:
        r = requests.post(url, json=params, timeout=10)
        data = r.json()
        if not data.get('ok'):
            log.warning(f"[TG] {method} 실패: {data.get('description')}")
        return data
    except Exception as e:
        log.error(f"[TG] {method} 오류: {e}")
        return {}


def _sb():
    """Supabase 클라이언트 반환. 없으면 예외."""
    if not _BRIDGE_OK:
        raise RuntimeError("Supabase 브릿지 없음")
    client = _bridge._get_client()
    if not client:
        raise RuntimeError("Supabase 클라이언트 연결 실패")
    return client


# ══════════════════════════════════════════════════════════════
# 📋  멤버 조회
# ══════════════════════════════════════════════════════════════

def get_members(active_only: bool = False) -> list:
    """pro_members 테이블 전체 조회. active_only=True면 is_active=True만."""
    sb = _sb()
    q = sb.table('pro_members').select('*').order('paid_until', desc=False)
    if active_only:
        q = q.eq('is_active', True)
    res = q.execute()
    return res.data or []


def get_member(telegram_id: int) -> Optional[dict]:
    """단건 조회. 없으면 None."""
    sb = _sb()
    try:
        res = sb.table('pro_members') \
                .select('*').eq('telegram_id', telegram_id).single().execute()
        return res.data
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# ➕  멤버 등록 / 연장
# ══════════════════════════════════════════════════════════════

def add_member(telegram_id: int, telegram_name: str = '',
               real_name: str = '', months: int = 1,
               memo: str = '') -> dict:
    """
    신규 멤버 등록.
    이미 존재하면 구독 연장으로 처리.
    반환: 저장된 row dict
    """
    sb = _sb()
    existing = get_member(telegram_id)
    today = date.today()

    if existing:
        # 이미 있으면 연장
        return extend_member(telegram_id, months)

    paid_until = today + timedelta(days=30 * months)
    row = {
        'telegram_id':   telegram_id,
        'telegram_name': telegram_name,
        'real_name':     real_name,
        'paid_until':    paid_until.isoformat(),
        'is_active':     True,
        'in_channel':    False,
        'memo':          memo,
    }
    res = sb.table('pro_members').insert(row).execute()
    data = (res.data or [{}])[0]
    log.info(f"[pro] 멤버 등록: {telegram_id} ({real_name}) — {paid_until}")
    return data


def extend_member(telegram_id: int, months: int = 1) -> dict:
    """
    구독 기간 연장.
    - 아직 만료 전: paid_until에서 +months
    - 이미 만료:    오늘부터 +months
    반환: 업데이트된 row dict
    """
    sb = _sb()
    existing = get_member(telegram_id)
    if not existing:
        raise ValueError(f"멤버 없음: {telegram_id}")

    current = date.fromisoformat(existing['paid_until'])
    base = max(current, date.today())           # 만료 시 오늘부터 재시작
    new_until = base + timedelta(days=30 * months)

    res = sb.table('pro_members').update({
        'paid_until':  new_until.isoformat(),
        'is_active':   True,
        'updated_at':  'now()',
    }).eq('telegram_id', telegram_id).execute()

    data = (res.data or [{}])[0]
    log.info(f"[pro] 구독 연장: {telegram_id} → {new_until} (+{months}개월)")
    return data


# ══════════════════════════════════════════════════════════════
# 🔗  초대 링크 생성 & 발송
# ══════════════════════════════════════════════════════════════

def create_invite_link(expire_hours: int = 48) -> Optional[str]:
    """
    1회용 초대 링크 생성.
    - member_limit=1, expire_date=지금+expire_hours
    반환: 링크 문자열 또는 None
    """
    channel_id = _pro_channel_id()
    expire_ts  = int(time.time()) + expire_hours * 3600

    res = _tg(
        'createChatInviteLink',
        chat_id      = channel_id,
        member_limit = 1,
        expire_date  = expire_ts,
    )
    if not res.get('ok'):
        return None
    return res.get('result', {}).get('invite_link')


def send_invite(telegram_id: int, months: int = None) -> bool:
    """
    멤버에게 1회용 초대 링크를 DM으로 발송.
    DB의 in_channel을 False → True(대기) 로 업데이트.
    반환: 발송 성공 여부
    """
    member = get_member(telegram_id)
    if not member:
        log.warning(f"[pro] send_invite: 멤버 없음 {telegram_id}")
        return False

    link = create_invite_link()
    if not link:
        log.error(f"[pro] 초대 링크 생성 실패 ({telegram_id})")
        return False

    paid_until = member['paid_until']
    name       = member.get('real_name') or member.get('telegram_name') or str(telegram_id)
    months_str = f" ({months}개월)" if months else ""

    msg = (
        f"🔐 <b>바티인베스트 프로 채널 초대 링크</b>{months_str}\n\n"
        f"안녕하세요, {name}님!\n"
        f"구독 감사드립니다. 아래 링크로 입장해 주세요.\n\n"
        f"🔗 {link}\n\n"
        f"📅 구독 만료일: <b>{paid_until}</b>\n"
        f"⚠️ 링크는 1회 사용 후 만료되며, 48시간 내 사용하지 않으면 자동 만료됩니다.\n\n"
        f"문의사항은 @batiinvest로 연락해 주세요."
    )

    res = _tg('sendMessage',
              chat_id    = telegram_id,
              text       = msg,
              parse_mode = 'HTML')

    if res.get('ok'):
        # DB: invite_sent 플래그 업데이트 (in_channel은 실제 입장 확인 후 업데이트)
        try:
            _sb().table('pro_members').update({
                'updated_at': 'now()',
                'memo':       (member.get('memo') or '') + f'\n[초대발송 {date.today()}]',
            }).eq('telegram_id', telegram_id).execute()
        except Exception:
            pass
        log.info(f"[pro] 초대 링크 발송 완료 → {telegram_id} ({name})")
        return True
    else:
        log.error(f"[pro] 초대 DM 발송 실패: {telegram_id}")
        return False


# ══════════════════════════════════════════════════════════════
# 🚪  강제 퇴장 (만료 처리)
# ══════════════════════════════════════════════════════════════

def kick_member(telegram_id: int, reason: str = '구독 만료') -> bool:
    """
    채널에서 멤버를 퇴장시킵니다.
    banChatMember → unbanChatMember 순서:
      ban  : 채널에서 즉시 강제 퇴장
      unban: 블랙리스트에서 제거 (나중에 재가입 가능)

    반환: 성공 여부
    """
    channel_id = _pro_channel_id()

    # 1. 강제 퇴장
    ban_res = _tg('banChatMember',
                  chat_id = channel_id,
                  user_id = telegram_id)

    # 2. 블랙리스트 해제 (재구독 가능하도록)
    time.sleep(0.5)
    _tg('unbanChatMember',
        chat_id         = channel_id,
        user_id         = telegram_id,
        only_if_banned  = True)

    success = ban_res.get('ok', False)

    if success:
        # DB 업데이트 — 기존 메모 먼저 조회 후 단일 update 호출 (이중 write 방지)
        try:
            current    = get_member(telegram_id)
            old_memo   = (current.get('memo') or '') if current else ''
            _sb().table('pro_members').update({
                'in_channel': False,
                'is_active':  False,
                'updated_at': 'now()',
                'memo':       old_memo + f'\n[퇴장 {date.today()} — {reason}]',
            }).eq('telegram_id', telegram_id).execute()
        except Exception as e:
            log.warning(f"[pro] kick DB 업데이트 실패: {e}")

        log.info(f"[pro] 퇴장 완료: {telegram_id} ({reason})")

        # 퇴장 알림 DM (선택적)
        try:
            _tg('sendMessage',
                chat_id    = telegram_id,
                text       = (
                    "📢 바티인베스트 프로 채널 구독이 만료되었습니다.\n\n"
                    "재구독을 원하시면 @batiinvest로 문의해 주세요.\n"
                    "감사합니다. 🙏"
                ),
                parse_mode = 'HTML')
        except Exception:
            pass
    else:
        log.warning(f"[pro] 퇴장 실패: {telegram_id} — {ban_res.get('description', '')}")

    return success


# ══════════════════════════════════════════════════════════════
# ⏰  만료 체크 (매일 자동 실행)
# ══════════════════════════════════════════════════════════════

def check_expired(admin_chat_id: str = None) -> dict:
    """
    오늘 기준으로 만료된 멤버를 퇴장시키고,
    N일 후 만료 예정인 멤버에게 사전 알림을 보냅니다.

    반환: {'kicked': [...], 'notified': [...], 'errors': [...]}
    """
    today       = date.today()
    notify_date = today + timedelta(days=_EXPIRE_NOTIFY_DAYS)
    result      = {'kicked': [], 'notified': [], 'errors': []}

    try:
        members = get_members(active_only=True)
    except Exception as e:
        log.error(f"[pro] 멤버 조회 실패: {e}")
        result['errors'].append(str(e))
        return result

    kicked_names    = []
    notified_names  = []

    for m in members:
        tid        = m['telegram_id']
        name       = m.get('real_name') or m.get('telegram_name') or str(tid)
        paid_until = date.fromisoformat(m['paid_until'])
        in_channel = m.get('in_channel', False)

        # ── 만료된 멤버 퇴장 ──────────────────────────────────
        if paid_until < today and in_channel:
            ok = kick_member(tid, reason=f"구독 만료 ({paid_until})")
            if ok:
                result['kicked'].append(tid)
                kicked_names.append(f"{name} ({paid_until})")
            else:
                result['errors'].append(f"퇴장 실패: {tid}")
            time.sleep(1)   # API 딜레이

        # ── 만료 D-3 사전 알림 ───────────────────────────────
        elif paid_until == notify_date and in_channel:
            try:
                _tg('sendMessage',
                    chat_id    = tid,
                    text       = (
                        f"⏰ <b>[구독 만료 {_EXPIRE_NOTIFY_DAYS}일 전 알림]</b>\n\n"
                        f"안녕하세요, {name}님!\n"
                        f"바티인베스트 프로 채널 구독이 "
                        f"<b>{paid_until}</b>에 만료됩니다.\n\n"
                        f"계속 이용을 원하시면 @batiinvest로 연락해 주세요."
                    ),
                    parse_mode = 'HTML')
                result['notified'].append(tid)
                notified_names.append(f"{name} ({paid_until})")
                log.info(f"[pro] 만료 예고 발송: {tid} ({name})")
            except Exception as e:
                log.warning(f"[pro] 만료 예고 발송 실패 {tid}: {e}")
            time.sleep(0.5)

    # ── 어드민 요약 알림 ─────────────────────────────────────
    if kicked_names or notified_names:
        lines = []
        if kicked_names:
            lines.append("🚪 <b>오늘 퇴장 처리</b>")
            lines += [f"  • {n}" for n in kicked_names]
        if notified_names:
            if lines: lines.append("")
            lines.append(f"⏰ <b>D-{_EXPIRE_NOTIFY_DAYS} 예고 발송</b>")
            lines += [f"  • {n}" for n in notified_names]

        summary = f"📋 <b>[프로 채널 구독 관리] {today}</b>\n\n" + "\n".join(lines)

        # 어드민 채팅방으로 발송
        _admin = admin_chat_id or _get_admin_chat()
        if _admin:
            _tg('sendMessage', chat_id=_admin, text=summary, parse_mode='HTML')

    log.info(
        f"[pro] 만료 체크 완료 — "
        f"퇴장 {len(result['kicked'])}명 / "
        f"예고 {len(result['notified'])}명 / "
        f"오류 {len(result['errors'])}건"
    )
    return result



# ══════════════════════════════════════════════════════════════
# 🔄  채널 상태 동기화
# ══════════════════════════════════════════════════════════════

def sync_channel_status(telegram_id: int, in_channel: bool) -> bool:
    """
    실제 채널 입장 여부를 DB에 반영.
    웹훅/폴링으로 chat_member_updated 이벤트를 받을 때 호출.
    """
    try:
        _sb().table('pro_members').update({
            'in_channel': in_channel,
            'updated_at': 'now()',
        }).eq('telegram_id', telegram_id).execute()
        log.info(f"[pro] in_channel 동기화: {telegram_id} → {in_channel}")
        return True
    except Exception as e:
        log.error(f"[pro] sync_channel_status 오류: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# 🛡️  관리자 헬퍼 (웹 대시보드 → 봇 트리거)
# ══════════════════════════════════════════════════════════════

def process_pro_action_flag():
    """
    app_config 'pro_action_flag' 를 polling해서
    대시보드에서 요청한 작업(초대발송, 수동퇴장)을 처리.

    run_all.py 메인 루프에서 60초마다 호출.
    flag 형식 (JSON):
      {
        "action":      "invite" | "kick" | "extend",
        "telegram_id": 12345678,
        "months":      1,           (extend 전용)
        "requested_at": "ISO timestamp"
      }
    """
    if not _BRIDGE_OK:
        return

    import json
    from datetime import timezone

    try:
        sb     = _bridge._get_client()
        req    = sb.table('app_config').select('value') \
                   .eq('key', 'pro_action_flag').single().execute()
        if not req.data or not req.data.get('value'):
            return

        raw = req.data['value']
        if not raw or raw == '{}':
            return

        data = json.loads(raw)
        action      = data.get('action')
        telegram_id = data.get('telegram_id')
        months      = data.get('months', 1)
        req_time    = data.get('requested_at', '')

        if not action or not telegram_id:
            return

        # 5분 이내 요청만 처리
        from datetime import datetime
        elapsed = (datetime.now(tz=timezone.utc) -
                   datetime.fromisoformat(req_time.replace('Z', '+00:00'))).total_seconds()
        if elapsed > 300:
            return

        log.info(f"[pro] 액션 플래그 감지: action={action} telegram_id={telegram_id}")

        # 플래그 먼저 초기화 (중복 실행 방지)
        sb.table('app_config').upsert(
            {'key': 'pro_action_flag', 'value': '{}'},
            on_conflict='key'
        ).execute()

        # 액션 실행
        if action == 'invite':
            send_invite(telegram_id, months=months)
        elif action == 'kick':
            kick_member(telegram_id, reason='관리자 수동 퇴장')
        elif action == 'extend':
            extend_member(telegram_id, months=months)
        else:
            log.warning(f"[pro] 알 수 없는 액션: {action}")

    except Exception as e:
        log.debug(f"pro_action_flag 체크 오류: {e}")


# ══════════════════════════════════════════════════════════════
# 📊  통계
# ══════════════════════════════════════════════════════════════

def get_stats() -> dict:
    """현황 통계 반환."""
    today = date.today()
    try:
        members  = get_members()
        total    = len(members)
        active   = sum(1 for m in members if m.get('is_active'))
        in_ch    = sum(1 for m in members if m.get('in_channel'))
        expiring = sum(1 for m in members
                       if m.get('is_active') and
                       date.fromisoformat(m['paid_until']) <= today + timedelta(days=7))
        expired  = sum(1 for m in members
                       if date.fromisoformat(m['paid_until']) < today)
        return {
            'total':    total,
            'active':   active,
            'in_channel': in_ch,
            'expiring_7d': expiring,
            'expired':  expired,
        }
    except Exception as e:
        log.error(f"[pro] get_stats 오류: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# 🚀  CLI 테스트용
# ══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'stats'

    if cmd == 'stats':
        print(get_stats())
    elif cmd == 'list':
        for m in get_members():
            print(f"  {m['telegram_id']:>12} | {m.get('real_name','?'):12} | {m['paid_until']} | in={m.get('in_channel')} | active={m.get('is_active')}")
    elif cmd == 'check':
        result = check_expired()
        print(f"퇴장 {len(result['kicked'])}명 / 예고 {len(result['notified'])}명 / 오류 {len(result['errors'])}건")
    elif cmd == 'invite':
        tid = int(sys.argv[2])
        ok = send_invite(tid)
        print("✅ 발송 성공" if ok else "❌ 발송 실패")
    else:
        print(f"사용법: python pro_channel.py [stats|list|check|invite <telegram_id>]")
