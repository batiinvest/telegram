"""
대량보유보고서 보고자명 API fallback 테스트
서버에서 실행: python3 scripts/test_large_holding.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_api
from dart_parser import get_disclosure_detail, _fetch_dart_reporter, _fetch_html, _build_kv

RCEPT_NO  = '20260608000324'
REPORT_NM = '주식등의대량보유상황보고서(일반)'
CHAT_ID   = '@batiinvest'

print('=== 1. DART API 보고자명 직접 조회 ===')
name = _fetch_dart_reporter(RCEPT_NO)
print(f'flr_nm: [{name}]')

print('\n=== 2. KV 전체 덤프 (비율/ratio 포함 키 강조) ===')
html = _fetch_html(RCEPT_NO)
kv = _build_kv(html) if html else {}
for k, v in kv.items():
    if k.startswith('_'):
        continue
    marker = '  <<<' if ('비율' in k or 'ratio' in k.lower() or '보고전' in k or '보고후' in k) else ''
    print(f'  [{k}] = [{v[:60]}]{marker}')

print('\n=== 3. 공시 상세 파싱 결과 ===')
detail = get_disclosure_detail(RCEPT_NO, REPORT_NM)
print(detail or '(결과 없음)')

print('\n=== 4. 텔레그램 전송 ===')
links = (
    f'🔗 <a href="http://dart.fss.or.kr/dsaf001/main.do?rcpNo={RCEPT_NO}">공시 원문</a> | '
    f'📈 <a href="https://finance.naver.com/item/main.nhn?code=310210">네이버</a>'
)
msg = (
    f'🧪 [테스트] 대량보유보고서 보고자명 파싱\n'
    f'rcept_no: {RCEPT_NO}\n\n'
    f'{detail or "(파싱 결과 없음)"}\n'
    f'{links}'
)
stock_api.send_telegram(CHAT_ID, msg)
print(f'전송 완료 → {CHAT_ID}')
