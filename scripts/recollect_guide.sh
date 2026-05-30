#!/bin/bash
# ============================================================
# 재무 데이터 전체 재수집 가이드
# 누적값 → 순수 분기값 변환 적용 버전
#
# [실행 전 필수] Supabase SQL Editor에서 먼저 실행:
#   DELETE FROM financials;
#   ALTER TABLE financials ADD COLUMN IF NOT EXISTS is_cumulative BOOLEAN DEFAULT FALSE;
#
# [중요] 단일 프로세스로 전체 연도/분기를 순서대로 수집
#        (Q4 변환 시 Q3 캐시를 재사용하기 위해 같은 프로세스에서 실행)
# ============================================================

cd /home/kjhofone

echo "=== 2023~2025 전분기 단일 프로세스 수집 시작 ==="
python3 collect_financials.py --from-year 2023 --monitored-only

echo ""
echo "=== 완료 후 검증 ==="
echo "python3 verify_financials.py --names 삼성전자 현대차 SK하이닉스 삼성SDI 두산에너빌리티"
