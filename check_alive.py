#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BatiInvest 외부 생존·리소스 감시 (20분 주기 cron)
─────────────────────────────────────────────────
봇 프로세스와 **독립**으로 돈다. 기존 알림은 전부 봇 자신이 보내기 때문에
import 에러 등으로 크래시 루프에 빠지면 알림 자체가 나가지 않았다.

점검:
  1) systemd bati_bot 서비스 활성 여부
  2) heartbeat.txt 갱신 지연 (메인 루프 정지 — 프로세스는 살아있는 행 상태 포함)
  3) 디스크 사용률 / 로그 디렉토리 용량

상태 플래그(.alive_state)로 같은 이상은 1회만 알리고, 회복 시 ✅ 1회 발송한다.
⚠️ app_config.heartbeat_*는 워치독이 4개를 한꺼번에 쓰므로 개별 봇 판정 근거로 쓰지 않는다.
"""
import os
import json
import time
import shutil
import subprocess
import requests
from dotenv import load_dotenv

load_dotenv('/home/kjhofone/.env')

HOME       = '/home/kjhofone'
STATE      = os.path.join(HOME, '.alive_state')
HEARTBEAT  = os.path.join(HOME, 'heartbeat.txt')
LOG_DIR    = os.path.join(HOME, 'logs')
TG_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
ADMIN_CHAT = os.getenv('ADMIN_CHAT_ID', '')

HEARTBEAT_MAX_AGE = 300          # 초 — 메인 루프는 60초마다 갱신
DISK_PCT_MAX      = 85
LOG_DIR_MAX_MB    = 500


def send(msg: str):
    if not (TG_TOKEN and ADMIN_CHAT):
        print(f'(텔레그램 미설정 — 발송 생략) {msg}')
        return
    try:
        requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                      json={'chat_id': ADMIN_CHAT, 'text': msg, 'parse_mode': 'HTML'},
                      timeout=15)
    except Exception as e:
        print(f'텔레그램 발송 실패: {e}')


def check():
    """{키: 이상 설명} — 정상이면 빈 dict."""
    bad = {}

    try:
        out = subprocess.run(['systemctl', 'is-active', 'bati_bot'],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        out = f'확인불가({e})'
    if out != 'active':
        bad['service'] = f'systemd bati_bot 상태: <code>{out}</code>'

    try:
        age = time.time() - os.path.getmtime(HEARTBEAT)
        if age > HEARTBEAT_MAX_AGE:
            bad['heartbeat'] = f'heartbeat.txt {int(age // 60)}분째 미갱신 (메인 루프 정지 의심)'
    except Exception as e:
        bad['heartbeat'] = f'heartbeat.txt 확인 불가: {e}'

    try:
        du = shutil.disk_usage('/')
        pct = du.used * 100 // du.total
        if pct >= DISK_PCT_MAX:
            bad['disk'] = f'디스크 사용률 {pct}% (여유 {du.free // (1024 ** 3)}GB)'
    except Exception:
        pass

    try:
        total = sum(os.path.getsize(os.path.join(LOG_DIR, f))
                    for f in os.listdir(LOG_DIR)
                    if os.path.isfile(os.path.join(LOG_DIR, f)))
        mb = total // (1024 ** 2)
        if mb >= LOG_DIR_MAX_MB:
            bad['logs'] = f'로그 디렉토리 {mb}MB (로테이션 확인 필요)'
    except Exception:
        pass

    return bad


def main():
    bad = check()
    try:
        prev = set(json.load(open(STATE, encoding='utf-8')))
    except Exception:
        prev = set()
    cur = set(bad)

    new = cur - prev
    if new:
        send('🚨 <b>[시스템 이상]</b>\n' + '\n'.join(f'• {bad[k]}' for k in sorted(new)))
    fixed = prev - cur
    if fixed:
        send('✅ <b>[시스템 회복]</b> ' + ', '.join(sorted(fixed)))

    try:
        json.dump(sorted(cur), open(STATE, 'w', encoding='utf-8'))
    except Exception as e:
        print(f'상태 저장 실패: {e}')

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 이상 {len(cur)}건"
          + (' — ' + '; '.join(bad[k] for k in sorted(cur)) if cur else ' (정상)'))


if __name__ == '__main__':
    main()
