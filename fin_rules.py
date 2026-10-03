"""
fin_rules.py — financials 테이블 해석 규칙 (단일 출처)
────────────────────────────────────────────────────
is_cumulative=true 행은 수집기가 '직전 분기가 없어 순분기로 못 바꾼' 행이다.
그 뜻은 분기마다 다르다 (2026-10-03 DART 원본 대조로 확정):

  - 1~3분기: 손익(매출·영업익·순익)은 DART '당분기'(thstrm_amount) = 분기 단독 그대로다.
    누적인 건 현금흐름뿐 → 손익 비교·표시에 그대로 써도 된다.
  - 4분기: 손익이 사업보고서 '연간값'이다. 대부분 상장 첫해라 DART에 3분기보고서가 없어
    원천에서 분기로 쪼갤 수 없다 → 분기 단독처럼 비교(QoQ·YoY)·표시하면 안 된다.

⚠️ 누적값에서 직전 분기 누계를 빼는 식의 처리는 1~3분기에선 틀린다(b703fca 오류 → 579c001 정정).
"""


def is_annual_q4(row, quarter=None) -> bool:
    """4분기 누적 행 = 손익이 연간값. quarter를 따로 주면 row['quarter'] 대신 쓴다
    (캐시 항목처럼 row에 분기 정보가 없을 때)."""
    if not row:
        return False
    q = quarter if quarter is not None else row.get('quarter')
    return str(q or '').upper().lstrip('Q') == '4' and bool(row.get('is_cumulative'))
