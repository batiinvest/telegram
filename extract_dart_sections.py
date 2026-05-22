#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DART 사업보고서 XML 섹션 추출 테스트 스크립트

사용 예시
    python3 extract_dart_sections.py dart_test_output/314930_바이오다인/document_xml/20260313000802.xml
    python3 extract_dart_sections.py dart_test_output/314930_바이오다인/document_xml/20260313000802_00761.xml

출력
    dart_test_output/314930_바이오다인/extracted_sections/{xml파일명}/
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from typing import Dict, List


def strip_tags(xml: str) -> str:
    # 표/문단/제목 단위 줄바꿈 보존
    xml = re.sub(r"</(TITLE|P|SPAN|TR|TABLE)>", "\n", xml, flags=re.I)
    xml = re.sub(r"</TD>", " | ", xml, flags=re.I)
    xml = re.sub(r"<[^>]+>", "", xml)
    xml = html.unescape(xml)
    xml = re.sub(r"[ \t]+", " ", xml)
    xml = re.sub(r"\n\s*\n+", "\n\n", xml)
    return xml.strip()


def title_text(title_tag: str) -> str:
    text = re.sub(r"<[^>]+>", "", title_tag)
    return html.unescape(text).strip()


def find_titles(xml: str) -> List[Dict[str, object]]:
    titles = []
    for m in re.finditer(r"<TITLE\b[^>]*>.*?</TITLE>", xml, flags=re.I | re.S):
        titles.append({
            "start": m.start(),
            "end": m.end(),
            "raw": m.group(0),
            "text": title_text(m.group(0)),
        })
    return titles


def extract_by_title_keyword(xml: str, keyword: str, stop_prefixes=None) -> str:
    titles = find_titles(xml)
    if not titles:
        return ""

    start_idx = None
    for i, t in enumerate(titles):
        if keyword in str(t["text"]):
            start_idx = i
            break

    if start_idx is None:
        return ""

    start_pos = int(titles[start_idx]["start"])
    end_pos = len(xml)

    if stop_prefixes:
        for t in titles[start_idx + 1:]:
            txt = str(t["text"])
            if any(txt.startswith(prefix) for prefix in stop_prefixes):
                end_pos = int(t["start"])
                break
    else:
        if start_idx + 1 < len(titles):
            end_pos = int(titles[start_idx + 1]["start"])

    return strip_tags(xml[start_pos:end_pos])


def extract_keyword_context(xml: str, keyword: str, window: int = 1800) -> str:
    plain = strip_tags(xml)
    matches = []
    for m in re.finditer(re.escape(keyword), plain):
        s = max(0, m.start() - window)
        e = min(len(plain), m.end() + window)
        matches.append(plain[s:e].strip())
    if not matches:
        return ""
    return "\n\n---\n\n".join(matches[:10])


def save_section(out_dir: Path, name: str, text: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.txt"
    path.write_text(text or "섹션을 찾지 못했습니다.", encoding="utf-8")
    print(f"[SAVE] {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xml_files", nargs="+", help="추출할 XML 파일 경로")
    args = parser.parse_args()

    for file in args.xml_files:
        xml_path = Path(file)
        xml = xml_path.read_text(encoding="utf-8", errors="replace")
        out_dir = xml_path.parent.parent / "extracted_sections" / xml_path.stem

        # 사업보고서 본문용
        save_section(out_dir, "II_사업의_내용", extract_by_title_keyword(xml, "II. 사업의 내용", stop_prefixes=["III."]))
        save_section(out_dir, "주요_제품_및_서비스", extract_by_title_keyword(xml, "주요 제품 및 서비스"))
        save_section(out_dir, "원재료_및_생산설비", extract_by_title_keyword(xml, "원재료 및 생산설비"))
        save_section(out_dir, "매출_및_수주상황", extract_by_title_keyword(xml, "매출 및 수주상황"))
        save_section(out_dir, "위험관리", extract_by_title_keyword(xml, "위험관리"))

        # 재무제표 주석/감사보고서 파일용 키워드 컨텍스트
        for keyword in ["해외매출", "수익인식", "우발부채", "특수관계자", "위험관리", "핵심감사사항"]:
            save_section(out_dir, f"키워드_{keyword}", extract_keyword_context(xml, keyword))


if __name__ == "__main__":
    main()
