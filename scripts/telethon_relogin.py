# -*- coding: utf-8 -*-
"""Telethon 관리자 계정 세션 재로그인 (미접속 강퇴·닉네임 검색 차단용).

대화식: 전화번호·인증 코드·2단계 비밀번호는 사용자가 터미널에서 직접 입력한다.
새 세션 문자열은 화면에 찍지 않고 .env 의 TELETHON_SESSION 줄만 교체한다(.env 백업 후).
끝나면 /tmp/telethon_relogin.done 에 결과(ok/fail, 계정 id)만 남긴다 — 비밀값 없음.
적용하려면 봇 재시작 필요(모듈이 import 시점에 세션을 읽음).

    python3 scripts/telethon_relogin.py
"""
import getpass
import os
import shutil
import sys
import time

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

ENV = "/home/kjhofone/.env"
DONE = "/tmp/telethon_relogin.done"
EXPECTED_ID = 533725430   # 바티 @BatiInvestment — 모든 방 관리자(강퇴 권한) 계정


def _done(msg):
    with open(DONE, "w") as f:
        f.write(msg + "\n")


def _save_session(s):
    bak = ENV + ".bak." + time.strftime("%Y%m%d%H%M%S")
    shutil.copy2(ENV, bak)
    os.chmod(bak, 0o600)
    with open(ENV, encoding="utf-8") as f:
        lines = f.read().split("\n")
    n = 0
    for i, ln in enumerate(lines):
        if ln.startswith("TELETHON_SESSION="):
            lines[i] = "TELETHON_SESSION=" + s
            n += 1
    if n == 0:
        lines.insert(len(lines) - 1 if lines and lines[-1] == "" else len(lines), "TELETHON_SESSION=" + s)
    tmp = ENV + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    shutil.copymode(ENV, tmp)
    os.replace(tmp, ENV)
    return bak


def main():
    if os.path.exists(DONE):
        os.remove(DONE)
    load_dotenv(ENV)
    api_id = int(os.environ["TELETHON_API_ID"])
    api_hash = os.environ["TELETHON_API_HASH"]

    print("텔레그램 관리자 계정(@BatiInvestment)으로 다시 로그인합니다.")
    print("인증 코드는 텔레그램 앱(또는 문자)으로 옵니다.\n")
    client = TelegramClient(StringSession(), api_id, api_hash)
    client.start(
        phone=lambda: input("전화번호 (+82 형식, 예 +8210...): ").strip(),
        code_callback=lambda: input("받은 인증 코드: ").strip(),
        password=lambda: getpass.getpass("2단계 인증 비밀번호 (화면에 안 보임): "),
    )
    me = client.get_me()
    s = client.session.save()
    client.disconnect()
    who = f"{me.first_name or ''} @{me.username or '-'} (id {me.id})"
    print(f"\n로그인됨: {who}")

    if me.id != EXPECTED_ID:
        ans = input(f"⚠️ 기대한 관리자 계정(id {EXPECTED_ID})이 아닙니다. 그래도 저장할까요? (y/N): ")
        if ans.strip().lower() != "y":
            _done(f"fail wrong_account {me.id}")
            print("저장하지 않았습니다.")
            return 1

    # 저장 전에 새 세션이 실제로 인증돼 있는지 확인
    chk = TelegramClient(StringSession(s), api_id, api_hash)
    chk.connect()
    ok = chk.is_user_authorized()
    chk.disconnect()
    if not ok:
        _done("fail not_authorized")
        print("새 세션 확인 실패 — 저장하지 않았습니다.")
        return 1

    bak = _save_session(s)
    _done(f"ok {me.id}")
    print(f"✅ 저장 완료 (.env 백업: {os.path.basename(bak)}). 이 창은 닫아도 됩니다.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        _done("fail cancelled")
        print("\n취소했습니다.")
        sys.exit(1)
    except Exception as e:
        _done("fail " + type(e).__name__)
        print(f"\n오류: {type(e).__name__}: {e}")
        sys.exit(1)
