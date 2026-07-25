"""
collect_company_info.py
───────────────────────
DART API → company_info 테이블 자동 수집

수집 항목:
  1. dart.company()    → 기업개황 (대표이사, 설립일, 결산월, 주소, 홈페이지)
  2. 사업보고서 XML   → 사업의 개요(B001), 주요 제품(B002)

실행:
  python collect_company_info.py --code 005930           # 단일 종목
  python collect_company_info.py --monitored             # 모니터링 종목 전체
  python collect_company_info.py --code 005930 --force   # 기존 데이터 덮어쓰기

API 호출 순서:
  1. companies 테이블에서 corp_code 조회
  2. dart.company(corp_code) → 기업개황
  3. dart.list(corp_code) 에서 최신 사업보고서 rcept_no 조회
  4. dart.document(rcept_no) → ZIP → XML 파싱 → B001/B002 섹션 추출
"""

import os
import sys
import io
import re
import time
from logger_config import get_logger
log = get_logger(__name__)

import zipfile
import argparse
from datetime import datetime

import requests
from dotenv import load_dotenv
load_dotenv()

try:
    from db_client import get_supabase_client as _get_sb
except ImportError:
    from supabase import create_client as _cs
    def _get_sb(): return _cs(os.getenv("SB_URL",""), os.getenv("SB_SERVICE_KEY",""))

try:
    import OpenDartReader
except ImportError:
    print("pip install opendartreader 필요"); sys.exit(1)


DART_API_KEY   = os.getenv("DART_API_KEY", "")
SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")
DART_DOC_URL   = "https://opendart.fss.or.kr/api/document.xml"


# ── 섹션 코드 ────────────────────────────────────────────────
# DART 사업보고서 XML 내부 섹션 코드
TARGET_SECTIONS = {
    "B001": "사업의 개요",
    "B002": "주요 제품 및 서비스",
}


# ── XML 텍스트 추출 ──────────────────────────────────────────
def _clean_text(raw: str) -> str:
    """XML/HTML 태그 제거 후 정제된 텍스트 반환"""
    # HTML 태그 제거
    text = re.sub(r'<[^>]+>', ' ', raw)
    # 특수문자 정규화
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&nbsp;', ' ').replace('&#160;', ' ')
    # 연속 공백/줄바꿈 정리
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def _extract_sections_from_xml(xml_content: str) -> dict:
    """
    사업보고서 XML에서 TARGET_SECTIONS 섹션 텍스트 추출.
    반환: {'B001': '사업의 개요 텍스트...', 'B002': '주요 제품...'}
    """
    result = {}

    # 섹션별 패턴: <섹션코드> ... </섹션코드> 또는 section 속성
    for code, name in TARGET_SECTIONS.items():
        # 방법 1: 섹션 코드 태그로 직접 추출
        pattern = rf'<{code}[^>]*>(.*?)</{code}>'
        match = re.search(pattern, xml_content, re.DOTALL | re.IGNORECASE)
        if match:
            text = _clean_text(match.group(1))
            if len(text) > 50:  # 의미 있는 텍스트만
                result[code] = text[:3000]  # 최대 3000자
                continue

        # 방법 2: 섹션명으로 검색 후 다음 섹션까지 추출
        name_pattern = rf'(?:{name}|{code})(.*?)(?=<[A-Z]\d{{3}}|$)'
        match2 = re.search(name_pattern, xml_content, re.DOTALL | re.IGNORECASE)
        if match2:
            text = _clean_text(match2.group(1))
            if len(text) > 50:
                result[code] = text[:3000]

    return result


def _fetch_business_report_xml(dart, corp_code: str) -> dict:
    """
    최신 사업보고서(연간)를 찾아 XML 섹션 텍스트 추출.
    반환: {'B001': str, 'B002': str} 또는 {}
    """
    try:
        # corp 파라미터로 기업 지정하여 사업보고서만 조회
        disc_list = dart.list(
            corp=corp_code,
            final=True,
        )
        if disc_list is None or disc_list.empty:
            log.warning(f"[{corp_code}] 사업보고서 목록 없음")
            return {}

        # 사업보고서(연간)만 필터
        if 'report_nm' in disc_list.columns:
            annual = disc_list[disc_list['report_nm'].str.contains('사업보고서', na=False)]
        else:
            annual = disc_list

        if annual.empty:
            log.warning(f"[{corp_code}] 사업보고서 없음")
            return {}

        rcept_no = annual.iloc[0].get('rcept_no', '')
        if not rcept_no:
            return {}

        log.info(f"  사업보고서 rcept_no: {rcept_no}")

        # ZIP 다운로드
        res = requests.get(
            DART_DOC_URL,
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            timeout=30,
        )
        if res.status_code != 200 or not res.content:
            log.warning(f"  사업보고서 ZIP 다운로드 실패: {res.status_code}")
            return {}

        # ZIP → XML 파싱
        try:
            zf = zipfile.ZipFile(io.BytesIO(res.content))
        except zipfile.BadZipFile:
            log.warning("  잘못된 ZIP 파일")
            return {}

        # XML 파일 목록에서 주요 문서 찾기
        xml_files = [f for f in zf.namelist() if f.endswith('.xml') and not f.startswith('__')]
        log.debug(f"  XML 파일: {xml_files}")

        combined_text = ""
        for xml_file in xml_files[:5]:  # 주요 파일 최대 5개
            try:
                raw = zf.read(xml_file)
                # 인코딩 감지
                for enc in ['utf-8', 'euc-kr', 'cp949']:
                    try:
                        combined_text += raw.decode(enc) + "\n"
                        break
                    except UnicodeDecodeError:
                        continue
            except Exception as e:
                log.debug(f"  {xml_file} 읽기 실패: {e}")

        if not combined_text:
            return {}

        return _extract_sections_from_xml(combined_text)

    except Exception as e:
        log.error(f"  사업보고서 XML 처리 오류: {e}")
        return {}


# ── 텍스트 → 마크다운 변환 ─────────────────────────────────
def _to_markdown_summary(text: str, section_name: str) -> str:
    """
    추출된 원문 텍스트를 기본 마크다운으로 정리.
    완전한 AI 요약은 아니고, 줄바꿈/제목 구조만 잡아줌.
    """
    if not text:
        return ""

    lines = text.split('\n')
    md_lines = [f"## {section_name}\n"]

    for line in lines:
        line = line.strip()
        if not line:
            md_lines.append("")
            continue
        # 짧은 줄(30자 미만)이면 소제목으로 처리
        if len(line) < 30 and not line.endswith('.') and not line.endswith(','):
            md_lines.append(f"### {line}")
        else:
            md_lines.append(line)

    return '\n'.join(md_lines)


# ── 단일 종목 수집 ───────────────────────────────────────────
def collect_one(dart, sb, stock_code: str, force: bool = False) -> bool:
    """
    단일 종목의 기업개황 + 사업보고서 내용을 company_info에 저장.

    Args:
        dart:       OpenDartReader 인스턴스
        sb:         Supabase 클라이언트
        stock_code: 6자리 종목코드
        force:      True면 기존 데이터 덮어쓰기

    Returns:
        성공 여부
    """
    # 기존 데이터 확인
    if not force:
        existing = sb.table('company_info').select('stock_code,updated_at') \
                     .eq('stock_code', stock_code).execute()
        if existing.data:
            log.info(f"[{stock_code}] 기존 데이터 있음 (force=False → 스킵)")
            return True

    # corp_code 조회
    co_res = sb.table('companies').select('corp_code,name') \
               .eq('code', stock_code).execute()
    if not co_res.data:
        log.warning(f"[{stock_code}] companies 테이블에 없음")
        return False

    corp_code = co_res.data[0].get('corp_code', '')
    corp_name = co_res.data[0].get('name', '')
    if not corp_code:
        log.warning(f"[{stock_code}] corp_code 없음")
        return False

    log.info(f"[{stock_code}] {corp_name} 수집 시작 (corp_code: {corp_code})")

    # ── 1. 기업개황 ──────────────────────────────────────────
    payload = {'stock_code': stock_code, 'corp_name': corp_name}
    try:
        info = dart.company(corp_code)
        if info is not None:
            payload.update({
                'corp_name_eng': info.get('corp_name_eng', ''),
                'ceo_nm':        info.get('ceo_nm', ''),
                'est_dt':        info.get('est_dt', ''),
                'acc_mt':        info.get('acc_mt', ''),
                'adres':         info.get('adres', ''),
                'hm_url':        info.get('hm_url', ''),
                'ir_url':        info.get('ir_url', ''),
                'induty_code':   info.get('induty_code', ''),
            })
            log.info(f"  기업개황: 대표이사={payload.get('ceo_nm')}, 설립={payload.get('est_dt')}")
    except Exception as e:
        log.warning(f"  기업개황 조회 실패: {e}")

    # ── 2. 사업보고서 XML → 사업 내용 ────────────────────────
    try:
        sections = _fetch_business_report_xml(dart, corp_code)
        if sections:
            # B001: 사업의 개요 → business_summary (앞 500자)
            if 'B001' in sections:
                raw = sections['B001']
                payload['business_summary'] = raw[:500]
                # 전체 내용은 business_detail_md에 마크다운으로 저장
                payload['business_detail_md'] = (
                    payload.get('business_detail_md', '') +
                    _to_markdown_summary(raw, '사업의 개요')
                )

            # B002: 주요 제품/서비스 → main_products
            if 'B002' in sections:
                raw = sections['B002']
                # 첫 몇 줄만 main_products로
                lines = [l.strip() for l in raw.split('\n') if l.strip()][:10]
                payload['main_products'] = '\n'.join(lines)
                payload['business_detail_md'] = (
                    payload.get('business_detail_md', '') + '\n\n' +
                    _to_markdown_summary(raw, '주요 제품 및 서비스')
                )

            log.info(f"  사업보고서 섹션 추출: {list(sections.keys())}")
        else:
            log.info(f"  사업보고서 섹션 없음 (수동 입력 필요)")
    except Exception as e:
        log.warning(f"  사업보고서 처리 실패: {e}")

    # ── 3. DB 저장 ────────────────────────────────────────────
    payload['updated_at'] = datetime.now().isoformat()
    try:
        sb.table('company_info') \
          .upsert(payload, on_conflict='stock_code') \
          .execute()
        log.info(f"  ✅ 저장 완료")
        return True
    except Exception as e:
        log.error(f"  ❌ 저장 실패: {e}")
        return False


# ── 배치 실행 ─────────────────────────────────────────────────
def run(stock_codes: list, force: bool = False) -> tuple[int, int]:
    """
    여러 종목 일괄 수집.
    Returns: (성공, 실패)
    """
    if not DART_API_KEY or not SB_URL or not SB_SERVICE_KEY:
        log.error("DART_API_KEY, SB_URL, SB_SERVICE_KEY 환경변수 필요")
        return 0, 0

    dart = OpenDartReader(DART_API_KEY)
    sb   = _get_sb()

    ok = fail = 0
    for i, code in enumerate(stock_codes, 1):
        log.info(f"[{i}/{len(stock_codes)}] {code}")
        try:
            success = collect_one(dart, sb, code, force=force)
            if success: ok += 1
            else:       fail += 1
        except Exception as e:
            log.error(f"  오류: {e}")
            fail += 1
        time.sleep(0.5)  # DART API rate limit

    log.info(f"완료: 성공 {ok}개 / 실패 {fail}개")
    return ok, fail


def run_monitored(force: bool = False) -> tuple[int, int]:
    """모니터링 종목 전체 수집"""
    if not SB_URL or not SB_SERVICE_KEY:
        log.error("SB_URL, SB_SERVICE_KEY 환경변수 필요")
        return 0, 0
    sb = _get_sb()
    res = sb.table('companies').select('code').eq('is_monitored', True).execute()
    codes = [r['code'].split('.')[0] for r in (res.data or []) if r.get('code')]
    log.info(f"모니터링 종목 {len(codes)}개 수집 시작")
    return run(codes, force=force)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DART 기업정보 수집")
    parser.add_argument("--code",      nargs="+",      help="종목코드 (예: 005930 000660)")
    parser.add_argument("--monitored", action="store_true", help="모니터링 종목 전체")
    parser.add_argument("--force",     action="store_true", help="기존 데이터 덮어쓰기")
    args = parser.parse_args()

    if args.monitored:
        run_monitored(force=args.force)
    elif args.code:
        run(args.code, force=args.force)
    else:
        parser.print_help()
