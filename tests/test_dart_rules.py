# -*- coding: utf-8 -*-
"""기존 루트 test_dart_rules.py(공시 분류·라우팅 52케이스)를 pytest 안전망에 편입.

원본은 __main__ 검증 스크립트라 함수 구조가 아니므로, 서브프로세스로 실행해
정상 종료 + '통과' 출력을 확인한다. (로직 중복 없이 기존 커버리지 보존)
UTF-8 강제 — 원본 마지막 print의 이모지가 Windows cp949에서 깨지는 것 회피.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_dart_rules_script_passes():
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "test_dart_rules.py")],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"exit={proc.returncode}\n{combined}"
    assert "통과" in combined, combined
    assert "FAIL" not in (proc.stdout or "").upper() or "통과" in combined
