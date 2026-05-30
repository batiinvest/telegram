#!/bin/bash
# ============================================================
# 전체 상장사 재무 데이터 초기 수집 스크립트
# 약 2,658개 종목 × 2023~2025 전분기
# 예상 소요시간: 40~70시간 (주말 실행 권장)
#
# [실행 전] Supabase에서 비모니터링 종목 데이터 확인:
#   SELECT COUNT(*) FROM financials f
#   JOIN companies c ON c.code = f.stock_code
#   WHERE c.is_monitored = false;
#
# [실행 방법]
#   nohup bash recollect_all.sh > recollect_all.log 2>&1 &
#   tail -f recollect_all.log
# ============================================================

cd /home/kjhofone

echo "=== 전체 상장사 재무 수집 시작: $(date) ==="
echo "대상: 비모니터링 종목 (모니터링 종목은 이미 수집됨)"
echo ""

# 비모니터링 종목만 수집 (모니터링 종목은 이미 완료)
python3 collect_financials.py --from-year 2023 --non-monitored-only

echo ""
echo "=== 완료: $(date) ==="
echo ""
echo "검증:"
echo "  python3 analyze_missing.py --col all"
