#!/usr/bin/env python3
"""
DART 파서 테스트 — 실제 공시 HTML에서 어떤 필드가 추출되는지 확인.

사용법:
    python scripts/test_dart_parser.py 20260601002040
    python scripts/test_dart_parser.py 20260601002040 --raw       # KV 원본 출력
    python scripts/test_dart_parser.py 20260601002040 --enc-debug # 인코딩 진단
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import requests
from dotenv import load_dotenv
load_dotenv()

from dart_parser import _fetch_html, _build_kv, parse_all_fields, get_disclosure_detail, _detect_encoding, _DESKTOP_UA


def _fetch_raw(rcept_no: str):
    """raw bytes + headers 반환 (인코딩 진단용)"""
    headers = {'User-Agent': _DESKTOP_UA}
    base = 'http://dart.fss.or.kr'
    import re
    idx = requests.get(f'{base}/dsaf001/main.do?rcpNo={rcept_no}', headers=headers, timeout=8)
    m = re.search(r'viewDoc\("(\d+)",\s*"(\d+)"', idx.content.decode('utf-8', errors='replace'))
    if not m:
        return None, None, None
    dcm_no = m.group(2)
    doc_url = (f'{base}/report/viewer.do?rcpNo={m.group(1)}&dcmNo={dcm_no}'
               f'&eleId=0&offset=0&length=0&dtd=HTML')
    doc = requests.get(doc_url, headers=headers, timeout=8)
    return doc.content, doc.headers, doc_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('rcept_no', help='공시 접수번호 (예: 20260601002040)')
    parser.add_argument('--raw',       action='store_true', help='KV 원본 전체 출력')
    parser.add_argument('--enc-debug', action='store_true', help='인코딩 진단 출력')
    parser.add_argument('--save-html', action='store_true', help='HTML을 파일로 저장')
    parser.add_argument('--tables',    action='store_true', help='테이블 행 구조 출력 (셀 수 + 내용)')
    parser.add_argument('--report-nm', default='', help='공시명 (선택)')
    args = parser.parse_args()

    print(f'\n{"="*60}')
    print(f'  rcept_no: {args.rcept_no}')
    if args.report_nm:
        print(f'  report_nm: {args.report_nm}')
    print(f'{"="*60}')

    # ── 인코딩 진단 ──────────────────────────────────────────
    if args.enc_debug:
        print('\n── 인코딩 진단 ──────────────────────────────')
        raw, resp_headers, doc_url = _fetch_raw(args.rcept_no)
        if raw is None:
            print('  ❌ viewDoc 패턴 없음')
            return

        enc = _detect_encoding(raw, resp_headers)
        ct  = resp_headers.get('Content-Type', '(없음)')
        print(f'  URL         : {doc_url}')
        print(f'  Content-Type: {ct}')
        print(f'  감지 인코딩 : {enc}')
        print(f'  raw 크기    : {len(raw):,} bytes')
        print(f'  첫 200 bytes: {raw[:200]}')
        print()

        # 각 인코딩으로 디코딩 후 한글 수 비교
        for try_enc in ('utf-8', 'euc-kr', 'cp949', 'utf-8-sig'):
            try:
                text = raw.decode(try_enc)
                kr   = sum(1 for c in text[:8000] if '가' <= c <= '힣')
                sample = text[200:300].replace('\n', ' ')
                print(f'  [{try_enc:<10}] 한글수={kr:4d}  샘플: {sample[:60]}')
            except Exception as e:
                print(f'  [{try_enc:<10}] 실패: {e}')
        print()

    # ── HTML 파싱 ─────────────────────────────────────────────
    html = _fetch_html(args.rcept_no)
    if not html:
        print('❌ HTML 가져오기 실패')
        return

    print(f'✅ HTML 길이: {len(html):,} chars')

    if args.save_html:
        fname = f'/tmp/dart_{args.rcept_no}.html'
        with open(fname, 'w', encoding='utf-8', errors='replace') as f:
            f.write(html)
        print(f'✅ HTML 저장: {fname}\n')

    if args.tables:
        import warnings
        try:
            from bs4 import XMLParsedAsHTMLWarning
            warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)
        except ImportError:
            pass
        from bs4 import BeautifulSoup
        import re as _re
        _html = html.replace('&cr;', ' ').replace('&nbsp;', ' ')
        soup = BeautifulSoup(_html, 'html.parser')

        print('── 테이블 행 구조 ─────────────────────────')
        for ti, table in enumerate(soup.find_all('table')):
            rows = table.find_all('tr')
            if not rows:
                continue
            print(f'\n  [Table {ti}] {len(rows)}행')
            for ri, row in enumerate(rows[:40]):
                all_cells = row.find_all(['td', 'th'])
                if not all_cells:
                    continue
                cells = []
                for c in all_cells:
                    t = _re.sub(r'\s+', ' ', c.get_text(' ', strip=True))[:50]
                    cells.append(t if t else '[빈셀]')
                print(f'    R{ri:02d} ({len(cells)}셀): {" | ".join(cells)}')
        print()

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
