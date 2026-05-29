"""
send_intro.py — 바티인베스트 채팅방 소개 메시지 전체 발송

rooms 테이블에서 채팅방 정보를 동적으로 읽어
산업별 섹션을 자동 구성한 뒤 전체 채팅방에 발송합니다.

━━━ 필요 컬럼 (rooms 테이블) ━━━
  name        TEXT    표시 이름 (종목명 / 산업명)
  code        TEXT    종목 코드 (종목 채팅방만, 산업 채팅방은 NULL)
  chat_id     TEXT    Telegram chat_id (봇 발송용)
  room_type   TEXT    'industry' | 'company' | 'main'
  cat         TEXT    산업 카테고리 (industry room은 cat = 산업명)
  invite_link TEXT    공개 초대 링크 (https://t.me/...)  ← 신규 추가
  status      TEXT    'open' | 'closed'   (정원 초과 여부)
  max_members INT     정원

━━━ 스키마 변경 SQL (최초 1회) ━━━
  ALTER TABLE rooms ADD COLUMN IF NOT EXISTS invite_link TEXT;

━━━ 사용법 ━━━
  python send_intro.py            # 전체 발송 (실제)
  python send_intro.py --dry-run  # 메시지 미리보기만 (발송 안 함)
  python send_intro.py --chat-id=-1001234567890  # 특정 채팅방만 테스트
"""

import os
import sys
import time
import logging
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

# ── 인자 파싱 ─────────────────────────────────────────────────────────────────
DRY_RUN    = '--dry-run' in sys.argv or '--preview' in sys.argv
TARGET_CID = next((a.split('=',1)[1] for a in sys.argv[1:] if a.startswith('--chat-id=')), None)

# ── Supabase ──────────────────────────────────────────────────────────────────
from supabase import create_client
_URL = os.environ['SUPABASE_URL']
_KEY = os.environ.get('SUPABASE_KEY', os.environ.get('SUPABASE_SERVICE_ROLE_KEY', ''))
sb   = create_client(_URL, _KEY)

# ── Telegram 봇 ───────────────────────────────────────────────────────────────
import requests
_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')

def _tg_send(chat_id: str, text: str, retry: int = 0):
    """HTML parse_mode로 Telegram 메시지 발송 (최대 3회 재시도)."""
    url = f'https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage'
    payload = {
        'chat_id': chat_id,
        'text':    text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        # HTML 파싱 오류 → plain text 재시도
        if r.status_code == 400 and 'parse' in r.text.lower() and retry == 0:
            log.warning(f'HTML 파싱 오류 ({chat_id}), plain text 재시도')
            payload.pop('parse_mode')
            return _tg_send(chat_id, text, retry=1)
        # 429 Rate limit → 대기 후 재시도
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

# ── 산업별 이모지 ─────────────────────────────────────────────────────────────
IND_EMOJI = {
    '바이오':   '💊',
    '뷰티':    '💄',
    '로봇':    '🤖',
    '2차전지':  '🔋',
    '신재생':   '☀️',
    '소비재':   '👗',
    '테크':    '💻',
    '반도체':   '💾',
    '엔터':    '🎤',
    '조선':    '🚢',
    '우주':    '🚀',
}

# 표시 순서
IND_ORDER = ['바이오','뷰티','로봇','2차전지','신재생','소비재','테크','반도체','엔터','조선','우주']

# ── DB 조회 ───────────────────────────────────────────────────────────────────
def load_rooms():
    """rooms 테이블 전체 로드."""
    res = sb.table('rooms').select(
        'name,code,chat_id,room_type,cat,invite_link,status,max_members'
    ).execute()
    return res.data or []

def load_industry_map():
    """companies 테이블에서 code → industry 매핑."""
    ind_map, offset = {}, 0
    while True:
        res = sb.table('companies').select('code,industry') \
                .not_.is_('industry', 'null') \
                .range(offset, offset + 999).execute()
        chunk = res.data or []
        for r in chunk:
            code = (r.get('code') or '').replace('.KS','').replace('.KQ','')
            if code and r.get('industry'):
                ind_map[code] = r['industry']
        if len(chunk) < 1000:
            break
        offset += 1000
    return ind_map

# ── 메시지 빌더 ───────────────────────────────────────────────────────────────
HEADER = """\
안녕하세요, 바티입니다.

바티인베스트는 건전한 투자 토론과 정보 공유를 위한 커뮤니티입니다. 광고 없는 클린한 환경 유지를 위해 아래 규정을 준수해 주세요.

✅ 입장 및 운영 안내
①승인: 신청 후 1~2일 내 순차 승인 (정원 초과 시 대기 발생)
②퇴장: 3일 이상 미접속(미활동), 광고/욕설/비매너 행위 시 즉시 퇴장
③우선입장: 후원자는 <a href="https://buymeacoffee.com/batiinvest">buymeacoffee</a> 대기없이 최우선 입장 안내

🔒 기타
① 각 채팅방은 정원에 따라 비공개로 전환될 수 있습니다.

📬 신규 채팅방 개설 문의
종목/산업 관련 신규 채팅방 개설 요청: @BatiInvestment


📊 종합 투자 채팅방
<a href="https://t.me/BatiInvestChat">바티인베스트</a>
• 미국/국내 시황 요약
• 공시·뉴스 모니터링 (실시간)
• 증권사 리포트·52주 신고가 (장 마감 후)
• 산업별 상승·하락률 순위 (장 마감 후)
"""

def _link(name: str, url: str | None) -> str:
    """링크 HTML. url 없으면 이름만."""
    if url:
        return f'<a href="{url}">{name}</a>'
    return name

def build_message(rooms: list, ind_map: dict) -> str:
    """전체 소개 메시지 생성."""

    # 산업 채팅방 맵: industry → {chat_id, invite_link, max_members}
    ind_rooms = {}
    for r in rooms:
        if r.get('room_type') == 'industry' and r.get('cat'):
            ind_rooms[r['cat']] = r

    # 종목 채팅방: industry → [room, ...]
    comp_by_ind = defaultdict(list)
    for r in rooms:
        if r.get('room_type') in (None, 'company') and r.get('code') and r.get('chat_id'):
            code = str(r['code']).replace('.KS','').replace('.KQ','')
            industry = ind_map.get(code, '')
            if industry:
                comp_by_ind[industry].append(r)
            # industry를 못 찾은 경우는 포함 안 함 (rooms.cat 등 별도 컬럼 있으면 확장 가능)

    lines = [HEADER]

    for ind in IND_ORDER:
        emoji   = IND_EMOJI.get(ind, '📌')
        ind_r   = ind_rooms.get(ind, {})
        cap     = ind_r.get('max_members', 1000)
        ind_url = ind_r.get('invite_link')
        comps   = sorted(comp_by_ind.get(ind, []), key=lambda x: x.get('name',''))

        ind_line = f'\n{emoji} {ind} 종목 채팅방 (정원: {cap:,}명)'
        if ind_url:
            ind_line += f'\n ➤ {_link(f"{ind} 산업 채팅방 바로가기", ind_url)}'
        else:
            ind_line += f'\n ➤ {ind} 산업 채팅방 (링크 미등록)'
        ind_line += '\n···········'
        lines.append(ind_line)

        if not comps:
            lines.append('   (등록된 종목 채팅방 없음)')
            continue

        closed = [r for r in comps if r.get('status') == 'closed']
        opened = [r for r in comps if r.get('status') != 'closed']

        if closed:
            lines.append('   [🔴정원 마감] ')
            lines.append('   ' + '   '.join(
                _link(r['name'], r.get('invite_link')) for r in closed
            ))

        if opened:
            lines.append('   [🟢입장 가능]  ')
            lines.append('   ' + '   '.join(
                _link(r['name'], r.get('invite_link')) for r in opened
            ))

    return '\n'.join(lines)

# ── 발송 ─────────────────────────────────────────────────────────────────────
def main():
    log.info('=== 소개 메시지 발송 시작 ===')

    rooms   = load_rooms()
    ind_map = load_industry_map()

    log.info(f'채팅방 {len(rooms)}개 로드, 산업 매핑 {len(ind_map)}개')

    message = build_message(rooms, ind_map)

    if DRY_RUN:
        print('\n' + '='*60)
        print(message)
        print('='*60)
        print(f'\n[DRY-RUN] 발송 없이 종료. 메시지 길이: {len(message)}자')
        return

    # 발송 대상 수집
    targets = []
    if TARGET_CID:
        targets = [TARGET_CID]
        log.info(f'테스트 모드: {TARGET_CID} 1개만 발송')
    else:
        seen = set()
        for r in rooms:
            cid = r.get('chat_id')
            if cid and cid not in seen:
                targets.append(cid)
                seen.add(cid)
        log.info(f'전체 발송 대상: {len(targets)}개 채팅방')

    ok = fail = 0
    for i, cid in enumerate(targets):
        if _tg_send(cid, message):
            ok += 1
            log.info(f'  [{i+1}/{len(targets)}] ✅ {cid}')
        else:
            fail += 1
            log.warning(f'  [{i+1}/{len(targets)}] ❌ {cid}')
        time.sleep(0.4)  # Telegram 속도 제한 방지

    log.info(f'=== 완료: 성공 {ok}개 / 실패 {fail}개 ===')

if __name__ == '__main__':
    main()
