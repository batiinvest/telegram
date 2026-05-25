"""
sms_parser.py — 한국 은행 입금 SMS 파싱
────────────────────────────────────────
주요 은행 입금 알림 문자를 파싱해 입금자명·금액·은행명을 추출합니다.

지원 은행:
  농협, 국민(KB), 신한, 우리, 하나(KEB), 기업(IBK),
  카카오뱅크, 토스뱅크, 케이뱅크, SC제일, 부산, 대구
"""

import re
from typing import Optional


# ──────────────────────────────────────────────────────────
# 은행별 패턴 (각 은행 SMS 형식 기반)
# ──────────────────────────────────────────────────────────
_BANK_PATTERNS = [
    # ── 카카오뱅크 ─────────────────────────────────────────
    # "카카오뱅크 홍길동님이 100,000원을 입금했어요."
    {
        'bank': '카카오뱅크',
        'pattern': r'카카오뱅크\s+(.+?)님이\s+([\d,]+)원을?\s*입금',
        'name_group': 1, 'amount_group': 2,
    },
    # ── 토스뱅크 ───────────────────────────────────────────
    # "[토스뱅크] 홍길동님이 100,000원을 보냈어요"
    {
        'bank': '토스뱅크',
        'pattern': r'\[?토스뱅크\]?\s+(.+?)님이\s+([\d,]+)원',
        'name_group': 1, 'amount_group': 2,
    },
    # ── 케이뱅크 ───────────────────────────────────────────
    # "[케이뱅크] 홍길동님이 100,000원을 입금"
    {
        'bank': '케이뱅크',
        'pattern': r'\[?케이뱅크\]?\s+(.+?)님이?\s+([\d,]+)원',
        'name_group': 1, 'amount_group': 2,
    },
    # ── 이름 먼저, 금액 나중 (농협/신한/우리/하나 등) ─────
    # "[농협] 홍길동 100,000원 입금 잔액 ..."
    {
        'bank': None,   # _detect_bank()로 추출
        'pattern': r'([가-힣]{2,5})\s+([\d,]+)원\s*입금',
        'name_group': 1, 'amount_group': 2,
    },
    # ── 금액 먼저, 이름 나중 (국민은행 일부 형식) ──────────
    # "[국민] 100,000원 입금 홍길동 잔액 ..."
    {
        'bank': None,
        'pattern': r'([\d,]+)원\s*입금\s+([가-힣]{2,5})',
        'name_group': 2, 'amount_group': 1,
    },
    # ── 외화 / 영문 포함 계좌이체 (보조 패턴) ─────────────
    # "HONG GIL DONG  KRW 100,000 입금"
    {
        'bank': None,
        'pattern': r'KRW\s+([\d,]+)\s*입금',
        'name_group': None, 'amount_group': 1,
    },
]

_BANK_KEYWORDS = {
    '농협':  ['농협', 'NH'],
    '국민':  ['국민', 'KB'],
    '신한':  ['신한', 'Shinhan'],
    '우리':  ['우리'],
    '하나':  ['하나', 'KEB', 'Hana'],
    '기업':  ['기업', 'IBK'],
    'SC':    ['SC', '제일'],
    '부산':  ['부산', 'BNK'],
    '대구':  ['대구', 'DGB'],
    '카카오뱅크': ['카카오', 'kakao'],
    '토스뱅크': ['토스', 'Toss'],
    '케이뱅크': ['케이뱅크'],
}


def _detect_bank(text: str) -> str:
    """SMS 텍스트에서 은행명 추출."""
    for bank, keywords in _BANK_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return bank
    return '알 수 없음'


def _clean_amount(raw: str) -> int:
    """'100,000' → 100000"""
    try:
        return int(raw.replace(',', '').replace(' ', ''))
    except ValueError:
        return 0


def parse_deposit(text: str) -> Optional[dict]:
    """
    입금 SMS를 파싱해 딕셔너리를 반환합니다.

    반환 형식:
      {
        'bank':   '농협',
        'name':   '홍길동',
        'amount': 100000,
        'raw':    '원본 SMS 텍스트',
      }
    또는 입금 SMS가 아니면 None.

    Parameters:
      text : 수신된 SMS 원문 (멀티라인 가능)
    """
    if not text:
        return None

    # 입금 관련 키워드가 없으면 조기 종료
    deposit_keywords = ['입금', '보냈어요', '보내셨어요', '입금했어요', '입금되었습니다']
    if not any(kw in text for kw in deposit_keywords):
        return None

    for p in _BANK_PATTERNS:
        m = re.search(p['pattern'], text, re.IGNORECASE)
        if not m:
            continue

        name   = m.group(p['name_group']).strip() if p['name_group'] else ''
        amount = _clean_amount(m.group(p['amount_group']))
        bank   = p['bank'] or _detect_bank(text)

        if amount <= 0:
            continue

        return {
            'bank':   bank,
            'name':   name,
            'amount': amount,
            'raw':    text.strip(),
        }

    return None


def is_bank_sms(sender: str) -> bool:
    """
    발신번호가 은행 번호인지 확인.
    SMS Forwarder 앱이 발신번호를 전달할 때 사용.
    """
    bank_numbers = [
        '15881006',   # 농협
        '15884000',   # 국민
        '15448000',   # 신한
        '15881111',   # 우리
        '18000500',   # 하나
        '15889999',   # 기업
        '15885000',   # 카카오뱅크
        '15880000',   # 토스뱅크
        '15991199',   # 케이뱅크
    ]
    clean = re.sub(r'[\-\s]', '', str(sender))
    return any(clean.endswith(bn) or clean == bn for bn in bank_numbers)


# ──────────────────────────────────────────────────────────
# 단위 테스트용 샘플
# ──────────────────────────────────────────────────────────
_TEST_SAMPLES = [
    ("[농협] 홍길동 100,000원 입금 잔액 1,234,567원",            {'bank': '농협', 'name': '홍길동', 'amount': 100000}),
    ("[국민] 50,000원 입금 김철수 잔액 500,000원",               {'bank': '국민', 'name': '김철수', 'amount': 50000}),
    ("[신한] 이영희 200,000원 입금 잔액 2,000,000원",            {'bank': '신한', 'name': '이영희', 'amount': 200000}),
    ("카카오뱅크 박민준님이 150,000원을 입금했어요. 잔액: 300,000원", {'bank': '카카오뱅크', 'name': '박민준', 'amount': 150000}),
    ("[토스뱅크] 최지원님이 80,000원을 보냈어요 잔액 120,000원",  {'bank': '토스뱅크', 'name': '최지원', 'amount': 80000}),
    ("[하나] 정수연 300,000원 입금 잔액 1,000,000원",             {'bank': '하나', 'name': '정수연', 'amount': 300000}),
]

if __name__ == '__main__':
    print("=== SMS 파싱 테스트 ===")
    ok, fail = 0, 0
    for text, expected in _TEST_SAMPLES:
        result = parse_deposit(text)
        passed = (result and
                  result['bank']   == expected['bank'] and
                  result['name']   == expected['name'] and
                  result['amount'] == expected['amount'])
        icon = "✅" if passed else "❌"
        if passed: ok += 1
        else: fail += 1
        print(f"{icon} [{expected['bank']}] {expected['name']} {expected['amount']:,}원")
        if not passed:
            print(f"   기대: {expected}")
            print(f"   결과: {result}")
    print(f"\n결과: {ok}/{ok+fail} 통과")
