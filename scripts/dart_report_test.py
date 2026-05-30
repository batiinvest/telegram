#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DART 사업보고서 원본 수집 테스트 스크립트 v0.2

사용 예시
    set -a
    source ~/.env
    set +a

    python3 dart_report_test.py --tickers 314930 --year 2025

출력
    ./dart_test_output/{ticker}_{corp_name}/

개선사항 v0.2
- --year 기준으로 사업보고서 제출연도 범위를 자동 설정
  예: --year 2025 -> 2026년에 제출된 "사업보고서 (2025.12)" 우선 선택
- report_nm 안의 "(YYYY.12)"와 --year를 우선 매칭
- 채무증권 발행실적 bdRs.json은 bgn_de/end_de 방식으로 별도 호출
- 잘못된 URL이 확인된 Ver 2.0 endpoint는 기본 비활성화
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    print("requests가 필요합니다. 먼저 실행하세요: pip install requests", file=sys.stderr)
    raise

BASE = "https://opendart.fss.or.kr/api"
REPORT_CODE_ANNUAL = "11011"  # 사업보고서


# 대부분의 정기보고서 주요정보 endpoint: corp_code + bsns_year + reprt_code
PERIODIC_INFO_ENDPOINTS_BY_YEAR: Dict[str, str] = {
    "증자_감자_현황": "irdsSttus.json",
    "배당에_관한_사항": "alotMatter.json",
    "자기주식_취득_처분_현황": "tesstkAcqsDspsSttus.json",
    "최대주주_현황": "hyslrSttus.json",
    "최대주주_변동현황": "hyslrChgSttus.json",
    "소액주주_현황": "mrhlSttus.json",
    "임원_현황": "exctvSttus.json",
    "직원_현황": "empSttus.json",
    "이사감사_개인별_보수현황_5억이상": "hmvAuditIndvdlBySttus.json",
    "이사감사_전체_보수현황": "hmvAuditAllSttus.json",
    "개인별_보수지급_상위5인": "indvdlByPay.json",
    "타법인_출자현황": "otrCprInvstmntSttus.json",
    "주식의_총수_현황": "stockTotqySttus.json",
    "기업어음증권_미상환_잔액": "entrprsBilScritsNrdmpBlce.json",
    "단기사채_미상환_잔액": "srtpdPsndbtNrdmpBlce.json",
    # 회사채 미상환 잔액 endpoint는 OpenDART 표기 변경/오타 가능성이 있어 후보를 자동 시도한다.
    # 정상 작동 endpoint가 확인되면 아래 CANDIDATE_COMPANY_BOND_ENDPOINTS 중 하나만 남기면 된다.
    "신종자본증권_미상환_잔액": "newCaplScritsNrdmpBlce.json",
    "조건부자본증권_미상환_잔액": "cndlCaplScritsNrdmpBlce.json",
    "회계감사인_명칭_감사의견": "accnutAdtorNmNdAdtOpinion.json",
    "감사용역_체결현황": "adtServcCnclsSttus.json",
    "비감사용역_계약체결현황": "accnutAdtorNonAdtServcCnclsSttus.json",
    "사외이사_변동현황": "outcmpnyDrctrNdChangeSttus.json",
    "미등기임원_보수현황": "unrstExctvMendngSttus.json",
    "보수현황_주총승인금액": "drctrAdtAllMendngSttusGmtsckConfmAmount.json",
    "보수현황_지급금액_유형별": "drctrAdtAllMendngSttusMendngPymntamtTyCl.json",
    "공모자금_사용내역": "pssrpCptalUseDtls.json",
    "사모자금_사용내역": "prvsrpCptalUseDtls.json",
}

# 기간 방식 endpoint: corp_code + bgn_de + end_de
PERIODIC_INFO_ENDPOINTS_BY_PERIOD: Dict[str, str] = {
    "채무증권_발행실적": "bdRs.json",
}

# 회사채 미상환 잔액 후보 endpoint.
# 실제 서버 실행 결과에서 status=000인 endpoint만 유지하면 된다.
CANDIDATE_COMPANY_BOND_ENDPOINTS = [
    "cmpnyDvdndNrdmpBlce.json",
    "cmpnyDvdndDdnsMnddtvSttus.json",
    "cmpnyBondNrdmpBlce.json",
]


def clean_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s.strip())
    return s[:80] or "unknown"


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def get_json(endpoint: str, params: Dict[str, Any], sleep: float = 0.15) -> Dict[str, Any]:
    url = f"{BASE}/{endpoint}"
    resp = requests.get(url, params=params, timeout=30)
    time.sleep(sleep)
    try:
        return resp.json()
    except Exception:
        return {
            "status": "HTTP_ERROR",
            "message": f"HTTP {resp.status_code}, JSON 파싱 실패",
            "endpoint": endpoint,
            "raw_text_head": resp.text[:500],
        }


def get_zip_bytes(endpoint: str, params: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[Dict[str, Any]]]:
    url = f"{BASE}/{endpoint}"
    resp = requests.get(url, params=params, timeout=60)
    content_type = resp.headers.get("Content-Type", "")
    content = resp.content

    if not content.startswith(b"PK"):
        text = content.decode("utf-8", errors="replace")[:1000]
        return None, {
            "status": "NOT_ZIP",
            "http_status": resp.status_code,
            "content_type": content_type,
            "message": text,
        }
    return content, None


def default_search_window_for_business_year(year: str) -> Tuple[str, str]:
    """
    사업보고서는 통상 해당 사업연도 다음 해에 제출된다.
    예: 2025년 사업보고서(2025.12)는 2026년에 제출.
    """
import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

    y = int(year)
    submit_year = y + 1
    today = dt.date.today()
    start = f"{submit_year}0101"
    # 제출연도가 미래면 넓게 오늘까지
    if submit_year > today.year:
        end = today.strftime("%Y%m%d")
    else:
        end = f"{submit_year}1231"
        # 현재 날짜가 제출연도 중간이면 오늘까지
        if submit_year == today.year:
            end = today.strftime("%Y%m%d")
    return start, end


def load_corp_codes(api_key: str, cache_dir: Path) -> List[Dict[str, str]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "corpCode.xml"

    if not cache_file.exists():
        zip_bytes, err = get_zip_bytes("corpCode.xml", {"crtfc_key": api_key})
        if err:
            raise RuntimeError(f"corpCode 다운로드 실패: {err}")
        zf = zipfile.ZipFile(BytesIO(zip_bytes))
        xml_name = zf.namelist()[0]
        cache_file.write_bytes(zf.read(xml_name))

    root = ET.fromstring(cache_file.read_bytes())
    corps: List[Dict[str, str]] = []
    for item in root.findall("list"):
        row = {child.tag: (child.text or "").strip() for child in item}
        corps.append(row)
    return corps


def find_corp_by_ticker(corps: List[Dict[str, str]], ticker: str) -> Optional[Dict[str, str]]:
    ticker = ticker.zfill(6)
    for row in corps:
        if row.get("stock_code") == ticker:
            return row
    return None


def select_annual_report(reports: List[Dict[str, Any]], business_year: str) -> Optional[Dict[str, Any]]:
    if not reports:
        return None

    annuals = [r for r in reports if "사업보고서" in r.get("report_nm", "")]
    if not annuals:
        annuals = reports

    # "사업보고서 (2025.12)"처럼 사업연도가 표시된 보고서를 우선 선택
    year_patterns = [
        f"({business_year}.12)",
        f"({business_year}. 12)",
        f"{business_year}.12",
        business_year,
    ]

    for pat in year_patterns:
        for r in annuals:
            if pat in r.get("report_nm", ""):
                return r

    # 그래도 없으면 접수일 최신순
    annuals.sort(key=lambda r: r.get("rcept_dt", ""), reverse=True)
    return annuals[0]


def search_annual_report(api_key: str, corp_code: str, begin_date: str, end_date: str, business_year: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    data = get_json("list.json", {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": begin_date,
        "end_de": end_date,
        "pblntf_ty": "A",
        "pblntf_detail_ty": "A001",
        "page_count": 100,
        "sort": "date",
        "sort_mth": "desc",
    })
    reports = data.get("list") or []
    return data, select_annual_report(reports, business_year)


def download_document_xml(api_key: str, rcept_no: str, out_dir: Path) -> Dict[str, Any]:
    zip_bytes, err = get_zip_bytes("document.xml", {"crtfc_key": api_key, "rcept_no": rcept_no})
    if err:
        save_json(out_dir / "document_error.json", err)
        return {"ok": False, "error": err}

    zip_path = out_dir / "document_raw.zip"
    zip_path.write_bytes(zip_bytes)

    zf = zipfile.ZipFile(BytesIO(zip_bytes))
    extracted = []
    for name in zf.namelist():
        data = zf.read(name)
        target = out_dir / "document_xml" / clean_filename(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        extracted.append(str(target))

    candidates = []
    for name in zf.namelist():
        data = zf.read(name)
        candidates.append((name, len(data), data))
    candidates.sort(key=lambda x: x[1], reverse=True)

    if candidates:
        name, _, data = candidates[0]
        text = data.decode("utf-8", errors="replace")
        save_text(out_dir / "document_preview.txt", text[:20000])
        return {"ok": True, "zip": str(zip_path), "files": extracted, "main_file": name}

    return {"ok": True, "zip": str(zip_path), "files": extracted}


def collect_financials(api_key: str, corp_code: str, year: str, out_dir: Path) -> None:
    params_base = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": year,
        "reprt_code": REPORT_CODE_ANNUAL,
    }

    data = get_json("fnlttSinglAcnt.json", params_base)
    save_json(out_dir / "financial_single_account.json", data)

    for fs_div in ["CFS", "OFS"]:
        data = get_json("fnlttSinglAcntAll.json", {**params_base, "fs_div": fs_div})
        save_json(out_dir / f"financial_all_{fs_div}.json", data)

    for idx_cl_code in ["M210000", "M220000", "M230000", "M240000"]:
        data = get_json("fnlttSinglIndx.json", {**params_base, "idx_cl_code": idx_cl_code})
        save_json(out_dir / f"financial_index_{idx_cl_code}.json", data)


def collect_periodic_info(
    api_key: str,
    corp_code: str,
    year: str,
    out_dir: Path,
    begin_date: str,
    end_date: str,
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {}

    params_year = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": year,
        "reprt_code": REPORT_CODE_ANNUAL,
    }

    for label, endpoint in PERIODIC_INFO_ENDPOINTS_BY_YEAR.items():
        data = get_json(endpoint, params_year)
        save_json(out_dir / f"{label}.json", data)
        summary[label] = {
            "endpoint": endpoint,
            "param_type": "bsns_year/reprt_code",
            "status": data.get("status"),
            "message": data.get("message"),
            "count": len(data.get("list") or []),
        }

    params_period = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": begin_date,
        "end_de": end_date,
    }

    for label, endpoint in PERIODIC_INFO_ENDPOINTS_BY_PERIOD.items():
        data = get_json(endpoint, params_period)
        save_json(out_dir / f"{label}.json", data)
        summary[label] = {
            "endpoint": endpoint,
            "param_type": "bgn_de/end_de",
            "status": data.get("status"),
            "message": data.get("message"),
            "count": len(data.get("list") or []),
        }

    # 회사채 미상환 잔액 후보 endpoint 자동 테스트
    company_bond_results = []
    best = None
    for endpoint in CANDIDATE_COMPANY_BOND_ENDPOINTS:
        data = get_json(endpoint, params_year)
        company_bond_results.append({
            "endpoint": endpoint,
            "status": data.get("status"),
            "message": data.get("message"),
            "count": len(data.get("list") or []),
        })
        save_json(out_dir / f"회사채_미상환_잔액__candidate__{clean_filename(endpoint)}.json", data)
        if data.get("status") == "000" and best is None:
            best = (endpoint, data)

    save_json(out_dir / "회사채_미상환_잔액_endpoint_candidates.json", company_bond_results)
    if best:
        endpoint, data = best
        save_json(out_dir / "회사채_미상환_잔액.json", data)
        summary["회사채_미상환_잔액"] = {
            "endpoint": endpoint,
            "param_type": "bsns_year/reprt_code",
            "status": data.get("status"),
            "message": data.get("message"),
            "count": len(data.get("list") or []),
        }
    else:
        summary["회사채_미상환_잔액"] = {
            "endpoint": "candidate_test_failed",
            "param_type": "bsns_year/reprt_code",
            "status": "CHECK_REQUIRED",
            "message": "후보 endpoint 모두 실패. 회사채 미상환 잔액 API URL 확인 필요.",
            "count": 0,
        }

    save_json(out_dir / "periodic_info_summary.json", summary)
    return summary


def make_markdown(
    out_dir: Path,
    ticker: str,
    corp: Dict[str, str],
    company: Dict[str, Any],
    report: Optional[Dict[str, Any]],
    periodic_summary: Dict[str, Any],
    year: str,
    begin_date: str,
    end_date: str,
) -> None:
    corp_name = corp.get("corp_name", "")
    report_nm = report.get("report_nm", "") if report else ""
    rcept_no = report.get("rcept_no", "") if report else ""
    rcept_dt = report.get("rcept_dt", "") if report else ""

    lines = []
    lines.append("---")
    lines.append(f"title: {corp_name} DART 원본 테스트")
    lines.append(f'ticker: "{ticker}"')
    lines.append(f'corp_code: "{corp.get("corp_code", "")}"')
    lines.append(f'business_year: "{year}"')
    lines.append(f'report_code: "{REPORT_CODE_ANNUAL}"')
    lines.append(f'rcept_no: "{rcept_no}"')
    lines.append(f'rcept_dt: "{rcept_dt}"')
    lines.append(f'search_begin_date: "{begin_date}"')
    lines.append(f'search_end_date: "{end_date}"')
    lines.append("status: raw-test")
    lines.append("---\n")

    lines.append(f"# {corp_name} DART 사업보고서 원본 테스트\n")
    lines.append("## 1. 기본 정보\n")
    lines.append("| 항목 | 값 |")
    lines.append("|---|---|")
    lines.append(f"| 종목코드 | {ticker} |")
    lines.append(f"| DART 고유번호 | {corp.get('corp_code', '')} |")
    lines.append(f"| 회사명 | {corp_name} |")
    lines.append(f"| 영문명 | {corp.get('corp_eng_name', '')} |")
    lines.append(f"| 사업연도 | {year} |")
    lines.append(f"| 공시검색 기간 | {begin_date} ~ {end_date} |")
    lines.append(f"| 선택 사업보고서 | {report_nm} |")
    lines.append(f"| 접수번호 | {rcept_no} |")
    lines.append(f"| 접수일 | {rcept_dt} |\n")

    lines.append("## 2. 기업개황 원자료 요약\n")
    lines.append("| 항목 | 값 |")
    lines.append("|---|---|")
    for key in [
        "corp_name", "corp_name_eng", "stock_name", "stock_code", "ceo_nm",
        "corp_cls", "adres", "hm_url", "ir_url", "phn_no", "fax_no",
        "induty_code", "est_dt", "acc_mt"
    ]:
        lines.append(f"| {key} | {company.get(key, '')} |")

    lines.append("\n## 3. 정기보고서 주요정보 호출 결과\n")
    lines.append("| 항목 | endpoint | params | status | message | rows |")
    lines.append("|---|---|---|---|---|---:|")
    for label, info in periodic_summary.items():
        lines.append(
            f"| {label} | {info.get('endpoint')} | {info.get('param_type')} | "
            f"{info.get('status')} | {info.get('message')} | {info.get('count')} |"
        )

    lines.append("\n## 4. 저장 파일\n")
    lines.append("- `company.json`")
    lines.append("- `disclosure_search.json`")
    lines.append("- `selected_report.json`")
    lines.append("- `document_raw.zip`")
    lines.append("- `document_xml/` 원문 XML 추출 파일")
    lines.append("- `financial/*.json`")
    lines.append("- `periodic_info/*.json`")

    lines.append("\n## 5. 다음 확인\n")
    lines.append("- `business_year`와 선택된 사업보고서의 연도 일치 여부 확인")
    lines.append("- `status=000` 정상 항목과 `013 조회된 데이터 없음` 항목 구분")
    lines.append("- `extracted_sections/`에 사업내용, 주요제품, 위험관리 등이 잘 추출되는지 확인")
    lines.append("- 회사채 미상환 잔액 endpoint 후보 테스트 결과 확인")
    lines.append("- 재무제표 연결/개별 중 어느 쪽을 기본값으로 쓸지 결정")

    save_text(out_dir / f"{ticker}_{clean_filename(corp_name)}_dart_raw_test.md", "\n".join(lines))


def run_for_ticker(api_key: str, ticker: str, year: str, output_root: Path, begin_date: str, end_date: str) -> None:
    ticker = ticker.zfill(6)
    print(f"\n=== {ticker} 처리 시작 ===")

    corps = load_corp_codes(api_key, output_root / "_cache")
    corp = find_corp_by_ticker(corps, ticker)
    if not corp:
        print(f"[FAIL] {ticker}: corp_code를 찾지 못했습니다.")
        return

    corp_name = corp.get("corp_name", "")
    corp_code = corp.get("corp_code", "")
    out_dir = output_root / f"{ticker}_{clean_filename(corp_name)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    save_json(out_dir / "corp_mapping.json", corp)

    print(f"[OK] corp_code: {corp_code}, corp_name: {corp_name}")

    company = get_json("company.json", {"crtfc_key": api_key, "corp_code": corp_code})
    save_json(out_dir / "company.json", company)
    print(f"[OK] 기업개황 status={company.get('status')} message={company.get('message')}")

    disclosure, report = search_annual_report(api_key, corp_code, begin_date, end_date, year)
    save_json(out_dir / "disclosure_search.json", disclosure)
    save_json(out_dir / "selected_report.json", report or {})

    if report:
        print(f"[OK] 선택 사업보고서: {report.get('report_nm')} / {report.get('rcept_dt')} / {report.get('rcept_no')}")
        doc_result = download_document_xml(api_key, report.get("rcept_no"), out_dir)
        save_json(out_dir / "document_download_result.json", doc_result)
        print(f"[OK] 원문 XML 다운로드 결과: {doc_result.get('ok')}")
    else:
        print("[WARN] 사업보고서를 찾지 못했습니다.")

    collect_financials(api_key, corp_code, year, out_dir / "financial")
    print("[OK] 재무정보 저장 완료")

    periodic_summary = collect_periodic_info(api_key, corp_code, year, out_dir / "periodic_info", begin_date, end_date)
    ok_count = sum(1 for v in periodic_summary.values() if v.get("status") == "000")
    print(f"[OK] 정기보고서 주요정보 저장 완료: 정상 {ok_count}/{len(periodic_summary)}")

    make_markdown(out_dir, ticker, corp, company, report, periodic_summary, year, begin_date, end_date)
    print(f"[DONE] 출력 폴더: {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=["314930"], help="테스트할 종목코드 목록")
    parser.add_argument("--year", default="2025", help="사업연도. 예: 2025")
    parser.add_argument("--begin-date", default=None, help="공시검색 시작일 YYYYMMDD. 기본: 사업연도 다음 해 0101")
    parser.add_argument("--end-date", default=None, help="공시검색 종료일 YYYYMMDD. 기본: 사업연도 다음 해 1231 또는 오늘")
    parser.add_argument("--output", default="dart_test_output", help="출력 폴더")
    args = parser.parse_args()

    api_key = os.getenv("DART_API_KEY") or os.getenv("OPENDART_API_KEY")
    if not api_key:
        raise SystemExit("환경변수 DART_API_KEY가 없습니다. 예: set -a && source ~/.env && set +a")

    if args.begin_date and args.end_date:
        begin_date, end_date = args.begin_date, args.end_date
    else:
        begin_date, end_date = default_search_window_for_business_year(args.year)

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    for ticker in args.tickers:
        try:
            run_for_ticker(api_key, ticker, args.year, output_root, begin_date, end_date)
        except Exception as e:
            print(f"[ERROR] {ticker}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
