# bot_requests.py — 대시보드 봇 요청 큐 처리 (bot_requests 테이블)
#
# 프론트엔드는 텔레그램 토큰 없이 bot_requests에 요청만 큐잉하고 상태를 폴링한다.
# 실제 Telegram API 호출(멤버 수 동기화·공지 발송)은 여기서 수행.
# run_all._run_watchdog_flags()가 매 60초 tick마다 process_bot_requests()를 호출.
#
# req_type / payload:
#   sync_all      {}                              — 전체 방 멤버 수 동기화
#   sync_one      {room_id}                       — 개별 방 동기화
#   notice        {target, content, parse_mode}   — 그룹/개별 공지
#                  target: all | open | 산업명 | room:ID | admin_direct | bati_direct
#   notice_single {room_id, content}              — 방 상세 모달 단건 발송 (HTML)
#   sync_desc     {room_id?}                      — 종목방 그룹 설명(소개글) 표준양식 일괄 교체
#   ping          {}                              — getMe 연결 테스트
#
# status: pending → processing → done | error   (result JSONB에 요약/오류)
# 테이블 미생성 시(sql/bot_requests.sql 실행 전) 조용히 스킵.

import os
import re
import time
import logging
import threading
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv('/home/kjhofone/.env')

from db_client import get_supabase_client

TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')

_running = False   # 동시 처리 방지 — 느린 sync_all이 다음 tick과 겹치지 않게


def _tg(method: str, params: dict):
    r = requests.post(
        f'https://api.telegram.org/bot{TG_TOKEN}/{method}',
        json=params, timeout=20,
    )
    d = r.json()
    if not d.get('ok'):
        raise RuntimeError(d.get('description', 'telegram error'))
    return d['result']


def _split_message(text: str, max_len: int = 4000):
    """4096자 한도 대비 줄 단위 분할 — 프론트 splitMessage와 동일 규칙"""
    if len(text) <= max_len:
        return [text]
    parts, cur = [], ''
    for line in text.split('\n'):
        nxt = (cur + '\n' + line) if cur else line
        if len(nxt) > max_len:
            if cur:
                parts.append(cur.strip())
            cur = line
        else:
            cur = nxt
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _resolve_targets(sb, target: str):
    """프론트 발송 대상 규칙과 동일: all | open | 산업명 | room:ID | admin_direct | bati_direct"""
    rooms = sb.table('rooms').select('id,name,chat_id,cat,status').execute().data or []
    if target.startswith('room:'):
        rid = int(target[5:])
        return [r for r in rooms if r['id'] == rid]
    if target == 'admin_direct':
        res = sb.table('app_config').select('value').eq('key', 'admin_chat_id').execute()
        cid = (res.data[0]['value'] if res.data else '').strip()
        return [{'chat_id': cid, 'name': cid}] if cid else []
    if target == 'bati_direct':
        return [{'chat_id': '@BatiInvestChat', 'name': '바티인베스트 채팅방'}]
    if target == 'all':
        return rooms
    if target == 'open':
        return [r for r in rooms if r.get('status') == 'open']
    return [r for r in rooms if r.get('cat') == target]


def _handle_sync(sb, requested_by=None, room_id=None):
    """멤버 수 동기화 — rooms.members 갱신 + sync_logs 기록 (기존 프론트 syncAll/syncOne 로직)"""
    q = sb.table('rooms').select('id,name,chat_id,members')
    if room_id:
        q = q.eq('id', room_id)
    rooms = q.execute().data or []
    if room_id and not rooms:
        raise ValueError(f'room_id {room_id} 없음')
    logs, updated, last_members = [], 0, None
    for r in rooms:
        try:
            c = _tg('getChatMemberCount', {'chat_id': r['chat_id']})
        except Exception as e:
            logging.warning(f"[BotReq] 멤버수 조회 실패 {r.get('name')}: {e}")
            c = None
        if c is not None:
            last_members = c
            if c != (r.get('members') or 0):
                sb.table('rooms').update({'members': c}).eq('id', r['id']).execute()
                log = {'room_id': r['id'], 'room_name': r['name'],
                       'before': r.get('members') or 0, 'after': c}
                if requested_by:
                    log['synced_by'] = requested_by
                logs.append(log)
                updated += 1
        time.sleep(0.15)
    if logs:
        sb.table('sync_logs').insert(logs).execute()
    if room_id and last_members is None:
        raise ValueError('조회 실패 — Chat ID 확인')
    result = {'total': len(rooms), 'updated': updated}
    if room_id:
        result['members'] = last_members
    return result


def _send_to_targets(sb, targets, content, parse_mode, history_target, requested_by=None):
    """대상 목록에 발송 — 수동 구분자(---) 분할 우선, 없으면 4096자 자동 분할. notice_history 기록."""
    manual = [s.strip() for s in re.split(r'\r?\n---\r?\n', content) if s.strip()]
    parts = manual if len(manual) > 1 else _split_message(content)
    ok = 0
    for t in targets:
        try:
            for i, p in enumerate(parts):
                _tg('sendMessage', {'chat_id': t['chat_id'], 'text': p, 'parse_mode': parse_mode})
                if i < len(parts) - 1:
                    time.sleep(0.5)
            ok += 1
        except Exception as e:
            logging.error(f"[BotReq] 공지 실패 {t.get('name')}: {e}")
        time.sleep(0.4)
    row = {'target': history_target, 'content': content,
           'sent_count': len(targets), 'ok_count': ok}
    if requested_by:
        row['sent_by'] = requested_by
    try:
        sb.table('notice_history').insert(row).execute()
    except Exception:
        # sent_by 컬럼 미존재 등 스키마 불일치 → 해당 필드 제거 후 1회 재시도
        row.pop('sent_by', None)
        try:
            sb.table('notice_history').insert(row).execute()
        except Exception as e2:
            logging.warning(f"[BotReq] notice_history 기록 실패: {e2}")
    return {'sent_count': len(targets), 'ok_count': ok, 'parts': len(parts)}


def _handle_notice(sb, payload, requested_by=None):
    target = (payload.get('target') or '').strip()
    content = (payload.get('content') or '').strip()
    parse_mode = payload.get('parse_mode') or 'HTML'
    if not content:
        raise ValueError('내용 없음')
    targets = _resolve_targets(sb, target)
    if not targets:
        raise ValueError('대상 채팅방 없음')
    return _send_to_targets(sb, targets, content, parse_mode, target, requested_by)


def _handle_notice_single(sb, payload, requested_by=None):
    room_id = payload.get('room_id')
    content = (payload.get('content') or '').strip()
    if not content:
        raise ValueError('내용 없음')
    rooms = sb.table('rooms').select('id,name,chat_id').eq('id', room_id).execute().data or []
    if not rooms:
        raise ValueError(f'room_id {room_id} 없음')
    r = rooms[0]
    return _send_to_targets(sb, [r], content, 'HTML', r['name'], requested_by)


DESC_TEMPLATE = """<{name} 채팅방>
📈 {name} 관련 정보를 실시간으로 공유하는 방입니다.
• 공시 및 뉴스 실시간 제공
• 시세 알림
• IR 자료 및 증권사 리포트

☕️ 후원: https://litt.ly/batiinvest
📬 문의: @BatiInvestment
⛔️ 퇴장 기준
① 광고·욕설·비하·반말·선동 등 비매너
② 3일 이상 미접속(미활동)
③ 규정 위반 시 즉시 퇴장"""


def _get_desc_template(sb):
    """소개글 템플릿 — app_config.room_desc_template 우선(대시보드 편집), 없으면 기본값."""
    try:
        res = sb.table('app_config').select('value').eq('key', 'room_desc_template').execute()
        v = ((res.data[0]['value'] if res.data else '') or '').strip()
        if v:
            return v
    except Exception as e:
        logging.warning(f"[BotReq] room_desc_template 조회 실패(기본값 사용): {e}")
    return DESC_TEMPLATE


def _handle_sync_desc(sb, payload):
    """종목 채팅방 그룹 설명(Description)을 표준 소개글로 일괄 교체.
    payload {room_id?} 지정 시 해당 방만, 없으면 room_type=company 전체."""
    only_room = payload.get('room_id')
    q = sb.table('rooms').select('id,name,chat_id,room_type')
    if only_room:
        q = q.eq('id', only_room)
    else:
        q = q.eq('room_type', 'company')
    rooms = q.execute().data or []
    tmpl = _get_desc_template(sb)
    ok = skip = fail = 0
    fails = []
    for r in rooms:
        name = r.get('name'); cid = r.get('chat_id')
        if not name or not cid:
            fail += 1; fails.append(f"{name or '?'}: chat_id 없음"); continue
        try:
            _tg('setChatDescription', {'chat_id': str(cid),
                                       'description': tmpl.replace('{name}', name)})
            ok += 1
        except Exception as e:
            if 'not modified' in str(e):
                skip += 1
            else:
                fail += 1; fails.append(f"{name}: {str(e)[:50]}")
        time.sleep(0.35)
    return {'total': len(rooms), 'ok': ok, 'skip': skip, 'fail': fail, 'fails': fails[:20]}


def _process_one(sb, req):
    rid = req['id']
    sb.table('bot_requests').update({'status': 'processing'}).eq('id', rid).execute()
    try:
        rtype = req.get('req_type')
        payload = req.get('payload') or {}
        by = req.get('requested_by')
        if rtype == 'sync_all':
            result = _handle_sync(sb, requested_by=by)
        elif rtype == 'sync_one':
            result = _handle_sync(sb, requested_by=by, room_id=payload.get('room_id'))
        elif rtype == 'notice':
            result = _handle_notice(sb, payload, by)
        elif rtype == 'notice_single':
            result = _handle_notice_single(sb, payload, by)
        elif rtype == 'sync_desc':
            result = _handle_sync_desc(sb, payload)
        elif rtype == 'ping':
            b = _tg('getMe', {})
            result = {'username': b.get('username'), 'first_name': b.get('first_name')}
        else:
            raise ValueError(f'알 수 없는 req_type: {rtype}')
        sb.table('bot_requests').update({
            'status': 'done', 'result': result,
            'processed_at': datetime.now(timezone.utc).isoformat(),
        }).eq('id', rid).execute()
        logging.info(f"✅ [BotReq] #{rid} {rtype} 완료: {result}")
    except Exception as e:
        try:
            sb.table('bot_requests').update({
                'status': 'error', 'result': {'error': str(e)[:500]},
                'processed_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', rid).execute()
        except Exception:
            pass
        logging.error(f"❌ [BotReq] #{rid} 실패: {e}")


def process_bot_requests():
    """워치독 tick 진입점 — pending 요청이 있으면 데몬 스레드로 순차 처리."""
    global _running
    if _running or not TG_TOKEN:
        return
    sb = get_supabase_client()
    try:
        # 봇 재시작 등으로 중단된 고아 processing 복구 — 15분 경과 시 error 처리
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        sb.table('bot_requests').update({
            'status': 'error',
            'result': {'error': '봇 재시작으로 처리 중단 — 일부만 발송됐을 수 있습니다. 필요 시 재발송하세요.'},
            'processed_at': datetime.now(timezone.utc).isoformat(),
        }).eq('status', 'processing').lt('created_at', cutoff).execute()
        res = sb.table('bot_requests').select('*') \
                .eq('status', 'pending').order('created_at').limit(10).execute()
    except Exception as e:
        # 테이블 미생성 등 — 조용히 스킵 (sql/bot_requests.sql 실행 전)
        logging.debug(f"[BotReq] 조회 실패(테이블 미생성?): {e}")
        return
    reqs = res.data or []
    if not reqs:
        return

    _running = True

    def _work():
        global _running
        try:
            for req in reqs:
                _process_one(sb, req)
        finally:
            _running = False

    threading.Thread(target=_work, name='Thread-BotRequests', daemon=True).start()


if __name__ == '__main__':
    # 수동 실행: pending 요청을 동기 처리 (스모크 테스트용)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    _sb = get_supabase_client()
    try:
        _res = _sb.table('bot_requests').select('*') \
                  .eq('status', 'pending').order('created_at').limit(10).execute()
        _reqs = _res.data or []
    except Exception as _e:
        print(f'조회 실패(테이블 미생성?): {_e}')
        _reqs = []
    for _req in _reqs:
        _process_one(_sb, _req)
    print(f'처리 완료: {len(_reqs)}건')
