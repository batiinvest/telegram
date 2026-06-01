#!/usr/bin/env python3
"""
DART 파서 테스트 — 실제 공시 HTML에서 어떤 필드가 추출되는지 확인.

사용법:
    python scripts/test_dart_parser.py 20260601002040
    python scripts/test_dart_parser.py 20260601002040 --raw   # KV 원본 출력
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
from dotenv import load_dotenv
load_dotenv()

from dart_parser import _fetch_html, _build_kv, parse_all_fields, get_disclosure_detail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('rcept_no', help='공시 접수번호 (예: 20260601002040)')
    parser.add_argument('--raw', action='store_true', help='KV 원본 전체 출력')
    parser.add_argument('--report-nm', default='', help='공시명 (선택)')
    args = parser.parse_args()

    print(f'\n{"="*60}')
    print(f'  rcept_no: {args.rcept_no}')
    if args.report_nm:
        print(f'  report_nm: {args.report_nm}')
    print(f'{"="*60}')

    html = _fetch_html(args.rcept_no)
    if not html:
        print('❌ HTML 가져오기 실패')
        return

    print(f'✅ HTML 길이: {len(html):,} chars')

    kv = _build_kv(html)
    print(f'✅ KV 추출: {len(kv)}개 필드\n')

    if args.raw:
        print('── KV 원본 ──────────────────────────────')
        for k, v in kv.items():
            v_disp = v[:120] + '…' if len(v) > 120 else v
            print(f'  [{k}] → {v_disp}')
        print()

    print('── parse_all_fields 결과 ────────────────')
    lines = parse_all_fields(kv)
    if lines:
        for line in lines:
            print(f'  {line}')
    else:
        print('  (결과 없음)')

    print()
    print('── get_disclosure_detail 결과 ───────────')
    detail = get_disclosure_detail(args.rcept_no, args.report_nm or '테스트')
    if detail:
        print(detail)
    else:
        print('  (결과 없음)')


if __name__ == '__main__':
    main()
