"""
sms_webhook.py — 입금 SMS 수신 웹훅 서버
──────────────────────────────────────────
SMS Forwarder 앱이 보낸 HTTP POST를 받아 입금 파싱 후 자동 처리합니다.

동작 흐름:
  1. 안드로이드 SMS Forwarder 앱이 입금 문자 수신
  2. 설정된 웹훅 URL로 POST 전송 (JSON or form)
  3. sms_parser.parse_deposit()로 파싱
  4. pro_members에서 입금자 이름으로 멤버 검색
  5. 매칭 성공 → 구독 갱신 + 초대 링크 DM 발송
  6. 매칭 실패 → 어드민에게 확인 요청 알림

엔드포인트:
  POST /sms          — SMS 수신 (인증 토큰 필요)
  GET  /sms/health   — 헬스 체크
  GET  /sms/deposits — 최근 처리 이력 (JSON)

설정 (app_config or 환경변수):
  sms_webhook_token   : 웹훅 인증 토큰 (Bearer or ?token=...)
  sms_default_months  : 입금 1회당 자동 연장 개월 수 (기본 1)
  sms_min_amount      : 최소 입금 금액 (기본 0, 비검증)

SMS Forwarder 앱 추천:
  - SmsForwarder (Android, GitHub: pppscn/SmsForwarder)
    → 채널: 自定义WebHook(Webhook), URL: http://서버IP:5001/sms?token=SECRET
  - SMS Gateway (Android)
    → Webhook URL: http://서버IP:5001/sms
    → Headers: Authorization: Bearer SECRET

실행:
  단독: python sms_webhook.py
  통합: run_all.py에서 스레드로 기동 (자동)
"""

import os
import re
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── 의존 임포트 ──────────────────────────────────────────────
try:
    from flask import Flask, request, jsonify
    _FLASK_OK = True
except ImportError:
    _FLASK_OK = False
    log.warning("Flask 없음 — pip install flask")

try:
    from sms_parser import parse_deposit
    _PARSER_OK = True
except ImportError:
    _PARSER_OK = False

try:
    import pro_channel as _pro
    import stock_api   as _sa
    _PRO_OK = True
except ImportError:
    _PRO_OK = False

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except ImportError:
    _BRIDGE_OK = False

# ── 설정 ─────────────────────────────────────────────────────
PORT               = int(os.getenv('SMS_WEBHOOK_PORT', 5001))
DEFAULT_MONTHS     = 1          # 입금 1회당 기본 연장 개월
_deposit_log: list = []         # 인메모리 처리 이력 (최대 100건)
_log_lock          = threading.Lock()

# ══════════════════════════════════════════════════════════════
# 🛠️  헬퍼
# ══════════════════════════════════════════════════════════════

def _get_config(key: str, default: str = '') -> str:
    if not _BRIDGE_OK:
        return default
    try:
        sb  = _bridge._get_client()
        res = sb.table('app_config').select('value').eq('key', key).single().execute()
        return (res.data or {}).get('value') or default
    except Exception:
        return default


def _get_admin_chat() -> Optional[str]:
    return _get_config('admin_chat_id') or None


def _check_token(req) -> bool:
    """Bearer 헤더 또는 ?token= 쿼리스트링으로 인증."""
    expected = _get_config('sms_webhook_token', '')
    if not expected:
        return True   # 토큰 미설정 시 인증 생략 (내부망 전용)

    auth_header = req.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:] == expected

    query_token = req.args.get('token', '')
    return query_token == expected


def _find_member_by_name(name: str) -> Optional[dict]:
    """real_name 또는 telegram_name으로 멤버 검색 (부분 일치)."""
    if not _PRO_OK or not name:
        return None
    try:
        members = _pro.get_members()
        name_clean = name.strip()
        # 1) 정확한 이름 일치
        for m in members:
            if m.get('real_name') == name_clean:
                return m
        # 2) 이름 포함
        for m in members:
            rn = m.get('real_name') or ''
            if name_clean in rn or rn in name_clean:
                return m
    except Exception as e:
        log.warning(f"[SMS] 멤버 검색 오류: {e}")
    return None


def _record_deposit(parsed: dict, member: Optional[dict], action: str):
    """처리 이력 기록 (인메모리 + Supabase app_config)."""
    entry = {
        'time':   datetime.now(timezone.utc).isoformat(),
        'bank':   parsed.get('bank', '?'),
        'name':   parsed.get('name', '?'),
        'amount': parsed.get('amount', 0),
        'member': member.get('real_name') if member else None,
        'action': action,
    }
    with _log_lock:
        _deposit_log.append(entry)
        if len(_deposit_log) > 100:
            _deposit_log.pop(0)

    # Supabase 저장
    if _BRIDGE_OK:
        try:
            sb  = _bridge._get_client()
            raw = json.dumps(_deposit_log[-20:], ensure_ascii=False)   # 최근 20건만
            sb.table('app_config').upsert(
                {'key': 'sms_deposit_log', 'value': raw, 'description': '입금 SMS 처리 이력'},
                on_conflict='key'
            ).execute()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# 🔑  입금 처리 로직
# ══════════════════════════════════════════════════════════════

def process_deposit(text: str, sender: str = '') -> dict:
    """
    SMS 텍스트를 파싱하고 입금 처리를 수행합니다.

    반환:
      {'status': 'matched'|'unmatched'|'not_deposit'|'error',
       'message': str, 'parsed': dict|None}
    """
    if not _PARSER_OK:
        return {'status': 'error', 'message': 'sms_parser 모듈 없음', 'parsed': None}

    # 1. 파싱
    parsed = parse_deposit(text)
    if not parsed:
        return {'status': 'not_deposit', 'message': '입금 SMS 아님', 'parsed': None}

    bank   = parsed['bank']
    name   = parsed['name']
    amount = parsed['amount']
    months = DEFAULT_MONTHS

    log.info(f"[SMS] 입금 파싱 성공: {bank} / {name} / {amount:,}원")

    admin_chat = _get_admin_chat()

    # 2. 멤버 검색
    member = _find_member_by_name(name)

    if member:
        tid = member['telegram_id']
        # ── 매칭 성공: 자동 구독 연장 + 초대 발송 ────────────
        try:
            _pro.extend_member(tid, months)
            invite_ok = _pro.send_invite(tid, months=months)
            action = 'auto_extended'

            msg = (
                f"✅ <b>[입금 자동 처리]</b>\n\n"
                f"🏦 {bank}  /  입금자: <b>{name}</b>  /  {amount:,}원\n"
                f"👤 멤버: {member.get('real_name')} (ID: {tid})\n"
                f"📅 구독 {months}개월 연장 완료\n"
                f"{'📨 초대 링크 DM 발송 완료' if invite_ok else '⚠️ 초대 링크 발송 실패 — 수동 발송 필요'}"
            )
            if admin_chat and _PRO_OK:
                _sa.send_telegram(admin_chat, msg)

            _record_deposit(parsed, member, action)
            log.info(f"[SMS] 자동 처리 완료: {name} → {tid}")
            return {'status': 'matched', 'message': '자동 처리 완료', 'parsed': parsed}

        except Exception as e:
            log.error(f"[SMS] 자동 처리 오류: {e}")
            action = 'error'
            if admin_chat and _PRO_OK:
                _sa.send_telegram(admin_chat,
                    f"⚠️ <b>[입금 처리 오류]</b>\n입금자: {name} / {amount:,}원\n오류: {e}")
            _record_deposit(parsed, member, action)
            return {'status': 'error', 'message': str(e), 'parsed': parsed}

    else:
        # ── 매칭 실패: 어드민에게 확인 요청 ─────────────────
        action = 'unmatched'
        if admin_chat and _PRO_OK:
            from datetime import date
            members_list = _pro.get_members(active_only=True)
            near_expire  = [
                m for m in members_list
                if not m.get('in_channel') or
                (date.fromisoformat(m['paid_until']) - date.today()).days < 3
            ]
            hint = ""
            if near_expire:
                hint = "\n\n💡 <b>최근 미입장/만료 근접 멤버:</b>\n" + "\n".join(
                    f"  • {m.get('real_name','?')} — ID: {m['telegram_id']}"
                    for m in near_expire[:5]
                )

            _sa.send_telegram(admin_chat,
                f"❓ <b>[미확인 입금 알림]</b>\n\n"
                f"🏦 {bank}  /  입금자: <b>{name}</b>  /  {amount:,}원\n\n"
                f"등록된 멤버와 이름이 일치하지 않습니다.\n"
                f"대시보드 → 봇 설정 → 프로 채널에서 직접 처리해 주세요."
                f"{hint}"
            )

        _record_deposit(parsed, None, action)
        log.info(f"[SMS] 미매칭 입금: {name} / {amount:,}원 → 어드민 알림")
        return {'status': 'unmatched', 'message': '멤버 미매칭', 'parsed': parsed}


# ══════════════════════════════════════════════════════════════
# 🌐  Flask 웹훅 서버
# ══════════════════════════════════════════════════════════════

def create_app() -> 'Flask':
    app = Flask(__name__)
    app.logger.setLevel(logging.WARNING)   # Flask 자체 로그 최소화

    @app.route('/sms', methods=['POST'])
    def receive_sms():
        """SMS Forwarder 앱에서 POST로 전달된 SMS를 처리합니다."""
        # ── 인증 ──────────────────────────────────────────────
        if not _check_token(request):
            log.warning(f"[SMS] 인증 실패 — IP: {request.remote_addr}")
            return jsonify({'error': 'Unauthorized'}), 401

        # ── SMS 텍스트 추출 ────────────────────────────────────
        # 지원 형식: JSON / form-data / plain text
        text   = ''
        sender = ''

        if request.is_json:
            data   = request.get_json(silent=True) or {}
            # SmsForwarder: {"msg": "...", "from": "..."} 또는 {"content": "...", "from": "..."}
            text   = (data.get('msg') or data.get('content') or
                      data.get('message') or data.get('text') or '')
            sender = data.get('from') or data.get('sender') or ''
        elif request.form:
            text   = (request.form.get('msg') or request.form.get('content') or
                      request.form.get('message') or request.form.get('text') or '')
            sender = request.form.get('from') or request.form.get('sender') or ''
        else:
            text = request.get_data(as_text=True) or ''

        if not text:
            return jsonify({'error': 'SMS 텍스트 없음'}), 400

        log.info(f"[SMS] 수신 ({sender}): {text[:80]}")

        # ── 처리 ──────────────────────────────────────────────
        result = process_deposit(text, sender)
        return jsonify(result), 200

    @app.route('/sms/health', methods=['GET'])
    def health():
        return jsonify({'status': 'ok', 'deposits': len(_deposit_log)}), 200

    @app.route('/sms/deposits', methods=['GET'])
    def deposits():
        """최근 처리 이력 반환 (최대 20건, 역순)"""
        if not _check_token(request):
            return jsonify({'error': 'Unauthorized'}), 401
        with _log_lock:
            return jsonify(list(reversed(_deposit_log[-20:]))), 200

    return app


# ══════════════════════════════════════════════════════════════
# 🚀  실행 진입점
# ══════════════════════════════════════════════════════════════

def run_server(host: str = '0.0.0.0', port: int = PORT):
    """Flask 서버를 시작합니다 (블로킹)."""
    if not _FLASK_OK:
        log.error("Flask 없음 — pip install flask 후 재시작")
        return
    app = create_app()
    log.info(f"🌐 [SMS 웹훅] 서버 시작 — http://{host}:{port}/sms")
    app.run(host=host, port=port, debug=False, use_reloader=False)


def start_thread(host: str = '0.0.0.0', port: int = PORT) -> threading.Thread:
    """별도 데몬 스레드로 웹훅 서버 기동 (run_all.py에서 호출)."""
    t = threading.Thread(
        target=run_server,
        args=(host, port),
        name="Thread-SmsWebhook",
        daemon=True,
    )
    t.start()
    return t


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    run_server()
