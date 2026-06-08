"""
대량보유보고서 보고자명 API fallback 테스트
서버에서 실행: python scripts/test_large_holding.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_api
from dart_parser import get_disclosure_detail, _fetch_dart_reporter

RCEPT_NO  = '20260608000324'
REPORT_NM = '주식등의대량보유상황보고서(일반)'
CHAT_ID   = '@BatiInvestChat'

print('=== 1. DART API 보고자명 직접 조회 ===')
name = _fetch_dart_reporter(RCEPT_NO)
print(f'flr_nm: [{name}]')

print('\n=== 2. 공시 상세 파싱 ===')
detail = get_disclosure_detail(RCEPT_NO, REPORT_NM)
print(detail or '(결과 없음)')

print('\n=== 3. 텔레그램 전송 ===')
msg = (
    f'🧪 [테스트] 대량보유보고서 보고자명 파싱\n'
    f'rcept_no: {RCEPT_NO}\n\n'
    f'{detail or "(파싱 결과 없음)"}'
)
stock_api.send_telegram(CHAT_ID, msg)
print(f'전송 완료 → {CHAT_ID}')
