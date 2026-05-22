#!/usr/bin/env python3
"""
verify_update.py — 배포 후 동작 검증 스크립트
서버에서 실행: cd /home/kjhofone && python3 verify_update.py

검증 항목:
  1. 모듈 import 정상 여부
  2. _get_industry_targets() 동작
  3. _broadcast_to_industries() / _broadcast_to_companies() 시그니처
  4. stock_api.get_company_chat_id() 동작 (종목명 직접 / fallback)
  5. news_main.py COMPANY_CHAT_IDS 직접 접근 제거 여부
  6. 산업 분석 함수 6개 _get_industry_targets 사용 여부
  7. job_* 함수들 for 루프 제거 여부
"""

import sys
import os
import inspect
import traceback

sys.path.insert(0, '/home/kjhofone')
os.chdir('/home/kjhofone')

from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"
results = []

def check(name, ok, detail=""):
    icon = PASS if ok else FAIL
    results.append((icon, name, detail))
    print(f"  {icon} {name}" + (f" — {detail}" if detail else ""))

print("\n" + "="*55)
print("  BatiInvest 배포 검증 스크립트")
print("="*55 + "\n")

# ── 1. 모듈 import ──────────────────────────────────────
print("[1] 모듈 import 검증")
try:
    import stock_api
    check("stock_api import", True)
except Exception as e:
    check("stock_api import", False, str(e))

try:
    import run_all
    check("run_all import", True)
except Exception as e:
    check("run_all import", False, str(e))

try:
    import news_main
    check("news_main import", True)
except Exception as e:
    check("news_main import", False, str(e))

try:
    import main as main_bot
    check("main (공시봇) import", True)
except Exception as e:
    check("main (공시봇) import", False, str(e))

# ── 2. get_company_chat_id() 존재 및 동작 ───────────────
print("\n[2] stock_api.get_company_chat_id() 검증")
try:
    fn = getattr(stock_api, 'get_company_chat_id', None)
    check("함수 존재", fn is not None, "stock_api.get_company_chat_id")

    if fn:
        # 없는 종목 → None 반환 확인
        result = fn("존재하지않는종목XYZ", "")
        check("없는 종목 → None 반환", result is None,
              f"반환값: {result}")

        # COMPANY_CHAT_IDS 내 종목 → 매핑 확인
        from config import COMPANY_CHAT_IDS, CHAT_IDS_BY_CODE
        check(f"COMPANY_CHAT_IDS 로드됨", len(COMPANY_CHAT_IDS) > 0,
              f"{len(COMPANY_CHAT_IDS)}개")
        check(f"CHAT_IDS_BY_CODE 로드됨", isinstance(CHAT_IDS_BY_CODE, dict),
              f"{len(CHAT_IDS_BY_CODE)}개")

        if COMPANY_CHAT_IDS:
            sample_name = next(iter(COMPANY_CHAT_IDS))
            result2 = fn(sample_name)
            check(f"종목명 직접 매칭 ({sample_name})", result2 is not None,
                  f"chat_id: {result2}")
except Exception as e:
    check("get_company_chat_id 실행", False, traceback.format_exc(limit=1))

# ── 3. main.py _get_company_chat_id 위임 확인 ─────────
print("\n[3] main.py DartRoutingBot._get_company_chat_id 위임 확인")
try:
    src = inspect.getsource(main_bot.DartRoutingBot._get_company_chat_id)
    uses_stock_api = 'stock_api.get_company_chat_id' in src
    has_own_logic  = 'COMPANY_CHAT_IDS[' in src or 'CHAT_IDS_BY_CODE.get' in src
    check("stock_api 위임 구조", uses_stock_api,
          "stock_api.get_company_chat_id 호출 확인")
    check("자체 로직 제거됨", not has_own_logic,
          "COMPANY_CHAT_IDS 직접 접근 없음")
except Exception as e:
    check("main.py 소스 검사", False, str(e))

# ── 4. news_main.py COMPANY_CHAT_IDS 직접 접근 제거 ────
print("\n[4] news_main.py 채팅방 라우팅 검증")
try:
    import ast
    with open('/home/kjhofone/news_main.py', 'r') as f:
        src = f.read()

    # COMPANY_CHAT_IDS[ 직접 subscript 없는지
    has_direct_access = 'COMPANY_CHAT_IDS[' in src
    check("COMPANY_CHAT_IDS 직접 접근 제거됨", not has_direct_access)

    # get_company_chat_id 사용 확인
    uses_helper = 'get_company_chat_id' in src
    check("get_company_chat_id 사용 확인", uses_helper)
except Exception as e:
    check("news_main.py 소스 검사", False, str(e))

# ── 5. _get_industry_targets 함수 존재 및 동작 ──────────
print("\n[5] stock_api._get_industry_targets() 검증")
try:
    fn = getattr(stock_api, '_get_industry_targets', None)
    check("함수 존재", fn is not None)

    if fn:
        # 없는 산업 → 에러 메시지 문자열 반환
        result = fn("존재하지않는산업XYZ")
        check("없는 산업 → 에러 문자열 반환", isinstance(result, str),
              f"반환: {result[:30]}...")

        # 있는 산업 → 튜플 반환
        from config import INDUSTRY_HIERARCHY
        if INDUSTRY_HIERARCHY:
            sample_ind = next(iter(INDUSTRY_HIERARCHY))
            result2 = fn(sample_ind)
            is_tuple = isinstance(result2, tuple) and len(result2) == 3
            check(f"유효한 산업 → 튜플 반환 ({sample_ind})", is_tuple,
                  f"타입: {type(result2).__name__}, 종목수: {len(result2[2]) if is_tuple else '?'}개")
except Exception as e:
    check("_get_industry_targets 실행", False, traceback.format_exc(limit=1))

# ── 6. 산업 분석 함수 6개 전처리 교체 확인 ──────────────
print("\n[6] 산업 분석 함수 중복 전처리 제거 확인")
INDUSTRY_FUNCS = [
    'get_sector_status',
    'get_sector_funds',
    'get_industry_weekly_ranking',
    'get_industry_cap_ranking',
    'get_industry_theme_ranking',
    'get_industry_financial_ranking',
]
try:
    for fname in INDUSTRY_FUNCS:
        fn = getattr(stock_api, fname, None)
        if not fn:
            check(f"{fname}", False, "함수 없음"); continue
        src = inspect.getsource(fn)
        uses_helper  = '_get_industry_targets' in src
        has_old_code = (
            'industry_name not in INDUSTRY_HIERARCHY' in src
            and '_get_industry_targets' not in src
        )
        check(
            f"{fname}",
            uses_helper and not has_old_code,
            "_get_industry_targets 사용 확인" if (uses_helper and not has_old_code)
            else "⚠️ 구버전 전처리 잔존"
        )
except Exception as e:
    check("산업 함수 소스 검사", False, str(e))

# ── 7. run_all.py for 루프 제거 + 헬퍼 사용 확인 ─────────
print("\n[7] run_all.py 브로드캐스트 헬퍼 검증")
BROADCAST_JOBS = [
    ('job_lunch_briefing',         'industry'),
    ('job_daily_closing',          'both'),
    ('job_saturday_industry_report','industry'),
    ('job_sunday_industry_recap',  'industry'),
    ('job_sunday_company_diagnosis','company'),
]
HELPER_IND = '_broadcast_to_industries'
HELPER_COM = '_broadcast_to_companies'
OLD_IND    = 'for ind_name, chat_id in INDUSTRY_CHAT_IDS'
OLD_COM    = 'for comp_name, chat_id in COMPANY_CHAT_IDS'

try:
    helpers_exist = (
        hasattr(run_all, '_broadcast_to_industries') and
        hasattr(run_all, '_broadcast_to_companies')
    )
    check("헬퍼 함수 2개 존재", helpers_exist)

    for job_name, kind in BROADCAST_JOBS:
        fn = getattr(run_all, job_name, None)
        if not fn:
            check(f"{job_name}", False, "함수 없음"); continue
        src = inspect.getsource(fn)

        has_old_ind = OLD_IND in src
        has_old_com = OLD_COM in src
        uses_ind    = HELPER_IND in src
        uses_com    = HELPER_COM in src

        if kind == 'industry':
            ok = uses_ind and not has_old_ind
            detail = "헬퍼 사용 ✓" if ok else f"old_loop={has_old_ind}, helper={uses_ind}"
        elif kind == 'company':
            ok = uses_com and not has_old_com
            detail = "헬퍼 사용 ✓" if ok else f"old_loop={has_old_com}, helper={uses_com}"
        else:  # both
            ok = uses_ind and uses_com and not has_old_ind and not has_old_com
            detail = "헬퍼 2개 사용 ✓" if ok else f"ind={uses_ind},com={uses_com},old_ind={has_old_ind},old_com={has_old_com}"

        check(f"{job_name}", ok, detail)
except Exception as e:
    check("run_all.py 소스 검사", False, str(e))

# ── 결과 요약 ────────────────────────────────────────────
print("\n" + "="*55)
total  = len(results)
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print(f"  결과: {passed}/{total} 통과  |  실패: {failed}개")
print("="*55 + "\n")

if failed > 0:
    print("실패 항목:")
    for icon, name, detail in results:
        if icon == FAIL:
            print(f"  {FAIL} {name}" + (f"\n     → {detail}" if detail else ""))

sys.exit(0 if failed == 0 else 1)
