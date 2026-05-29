"""
send_intro.py — 바티인베스트 채팅방 소개 메시지 전체 발송

rooms 테이블에서 link·cat·status·members·max_members를 읽어
프론트엔드 autoGenNotice()와 동일한 메시지를 생성 후 전체 채팅방 발송.

rooms 테이블 컬럼 (기존 그대로 사용, 스키마 변경 불필요):
  name        TEXT    표시 이름
  chat_id     TEXT    Telegram chat_id (봇 발송용)
  room_type   TEXT    'industry' = 산업 채팅방, 나머지 = 종목 채팅방
  cat         TEXT    산업 카테고리 (industry·company 모두)
  link        TEXT    공개 초대 링크 (https://t.me/...)
  status      TEXT    'full' = 정원 마감, 그 외 = 입장 가능
  members     INT     현재 인원
  max_members INT     정원

사용법:
  python send_intro.py            # 전체 발송
  python send_intro.py --dry-run  # 메시지만 출력, 발송 안 함
  python send_intro.py --chat-id=<id>  # 특정 채팅방만 테스트 발송
"""

import os, sys, time, logging
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

DRY_RUN    = '--dry-run' in sys.argv or '--preview' in sys.argv
TARGET_CID = next((a.split('=', 1)[1] for a in sys.argv[1:] if a.startswith('--chat-id=')), None)

# ── Supabase ──────────────────────────────────────────────────────────────────
from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'],
                   os.environ.get('SUPABASE_KEY', os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')))

# ── Telegram 발송 ─────────────────────────────────────────────────────────────
import requests
_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')

def _tg_send(chat_id: str, text: str, retry: int = 0) -> bool:
    url  = f'https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage'
    data = {'chat_id': chat_id, 'text': text,
            'parse_mode': 'HTML', 'disable_web_page_preview': True}
    try:
        r = requests.post(url, json=data, timeout=10)
        if r.status_code == 200:
            return True
        if r.status_code == 400 and 'parse' in r.text.lower() and retry == 0:
            log.warning(f'HTML 파싱 오류 ({chat_id}), plain text 재시도')
            data.pop('parse_mode')
            return _tg_send(chat_id, text, retry=1)
        if r.status_code == 429 and retry < 3:
            wait = int(r.json().get('parameters', {}).get('retry_after', 5))
            log.warning(f'Rate limit → {wait}s 대기')
            time.sleep(wait)
            return _tg_send(chat_id, text, retry + 1)
        log.error(f'발송 실패 ({chat_id}): {r.status_code} {r.text[:200]}')
        return False
    except Exception as e:
        log.error(f'발송 예외 ({chat_id}): {e}')
        return False

# ── DB 조회 ───────────────────────────────────────────────────────────────────
def load_rooms() -> list:
    res = sb.table('rooms').select(
        'id,name,chat_id,room_type,cat,link,status,members,max_members'
    ).order('cat').order('name').execute()
    return res.data or []

# ── 메시지 빌더 (rooms.js autoGenNotice 로직 동일) ─────────────────────────────
IND_EMOJI = {
    '바이오': '💊', '반도체': '💾', '2차전지': '🔋', '로봇': '🤖',
    '조선':   '🚢', '우주':   '🚀', '신재생':  '☀️', '테크': '💻',
    '엔터':   '🎤', '뷰티':   '💄', '소비재':  '👗',
}

def _link(r: dict) -> str:
    """HTML 링크. link 없으면 이름만."""
    if r.get('link'):
        return f'<a href="{r["link"]}">{r["name"]}</a>'
    return r['name']

def build_message(rooms: list) -> str:
    # 종목 채팅방만 (industry 제외)
    comp_rooms = [r for r in rooms if r.get('room_type') != 'industry']

    # 정원 마감 여부
    def is_full(r):
        return r.get('status') == 'full' or (r.get('members') or 0) >= (r.get('max_members') or 1000)

    # 산업별 그룹화
    grouped: dict[str, dict] = {}
    for r in comp_rooms:
        cat = r.get('cat') or '기타'
        if cat not in grouped:
            grouped[cat] = {'full': [], 'open': []}
        grouped[cat]['full' if is_full(r) else 'open'].append(r)

    total_rooms = len(comp_rooms)
    open_count  = sum(1 for r in comp_rooms if not is_full(r))
    full_count  = total_rooms - open_count

    # 산업 채팅방 맵
    ind_rooms = {
        r['cat']: r
        for r in rooms
        if r.get('room_type') == 'industry' and r.get('cat')
    }

    lines = []

    # ── 헤더 ──────────────────────────────────────────────────────────────────
    lines += [
        '안녕하세요, <b>바티인베스트</b>입니다 👋',
        '',
        '건전한 투자 토론과 정보 공유를 위한 종목별 채팅방을 안내드립니다.',
        f'현재 <b>{total_rooms}개</b> 채팅방 운영 중 — 🟢 입장 가능 {open_count}개 · 🔴 정원 마감 {full_count}개',
        '',
    ]

    # 종합 채팅방
    main_room = next(
        (r for r in rooms if r.get('room_type') == 'industry' and '바티인베스트' in (r.get('name') or '')),
        None
    )
    if main_room and main_room.get('link'):
        lines.append(f'<a href="{main_room["link"]}">📊 바티인베스트 종합 채팅방</a> — 시황·공시·리포트 실시간')
        lines.append('')

    lines.append('─' * 22)
    lines.append('')

    # ── 산업별 섹션 ───────────────────────────────────────────────────────────
    # 종목 수 내림차순 정렬
    cats = sorted(grouped.keys(),
                  key=lambda c: len(grouped[c]['full']) + len(grouped[c]['open']),
                  reverse=True)

    for idx, cat in enumerate(cats):
        full = grouped[cat]['full']
        open_ = grouped[cat]['open']
        emoji = IND_EMOJI.get(cat, '📌')
        total = len(full) + len(open_)
        ind_r = ind_rooms.get(cat)

        if idx > 0:
            lines.append('')

        lines.append(f'{emoji} <b>{cat}</b> ({total}개)')
        if ind_r and ind_r.get('link'):
            lines.append(f'└ <a href="{ind_r["link"]}">{cat} 산업방</a>')
        lines.append('')

        for r in sorted(full,  key=lambda x: x.get('name', '')):
            lines.append(f'🔴 {_link(r)}')
        for r in sorted(open_, key=lambda x: x.get('name', '')):
            lines.append(f'🟢 {_link(r)}')

    # ── 하단 안내 ─────────────────────────────────────────────────────────────
    lines += [
        '',
        '─' * 22,
        '',
        '✅ <b>입장 안내</b>',
        '· 승인 후 1~2일 내 순차 입장',
        '· 3일 이상 미접속 시 자동 퇴장',
        '· 후원자 우선 입장: buymeacoffee.com/batiinvest',
        '',
        '📬 신규 채팅방 개설 문의: @BatiInvestment',
    ]

    return '\n'.join(lines).strip()

# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    log.info('=== 소개 메시지 발송 시작 ===')
    rooms = load_rooms()
    log.info(f'채팅방 {len(rooms)}개 로드')

    message = build_message(rooms)

    if DRY_RUN:
        print('\n' + '=' * 60)
        print(message)
        print('=' * 60)
        log.info(f'[DRY-RUN] 발송 없이 종료. 메시지 길이: {len(message)}자')
        return

    # 발송 대상
    if TARGET_CID:
        targets = [TARGET_CID]
        log.info(f'테스트 모드: {TARGET_CID} 1개만 발송')
    else:
        seen, targets = set(), []
        for r in rooms:
            cid = r.get('chat_id')
            if cid and cid not in seen:
                targets.append(cid)
                seen.add(cid)
        log.info(f'전체 발송 대상: {len(targets)}개')

    ok = fail = 0
    for i, cid in enumerate(targets):
        if _tg_send(cid, message):
            ok += 1
            log.info(f'  [{i+1}/{len(targets)}] ✅ {cid}')
        else:
            fail += 1
            log.warning(f'  [{i+1}/{len(targets)}] ❌ {cid}')
        time.sleep(0.4)

    log.info(f'=== 완료: 성공 {ok}개 / 실패 {fail}개 ===')

if __name__ == '__main__':
    main()
