#!/usr/bin/env bash
set -e

# DART API 키 로드
set -a
source ~/.env
set +a

echo "DART_API_KEY length: ${#DART_API_KEY}"

# 1개 종목 테스트: 바이오다인 2025년 사업보고서
python3 dart_report_test.py --tickers 314930 --year 2025

# XML 섹션 추출
MAIN_XML=$(find dart_test_output/314930_바이오다인/document_xml -maxdepth 1 -name "*.xml" | sort | head -n 1)
echo "MAIN_XML=$MAIN_XML"

python3 extract_dart_sections.py "$MAIN_XML"

# 결과 파일 확인
find dart_test_output/314930_바이오다인 -maxdepth 3 -type f | sort | head -n 80
