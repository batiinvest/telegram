#!/usr/bin/env python3
"""
DART Q2/Q3가 누적값인지 단독값인지 종목별로 판별하는 스크립트
사용법: python3 check_cumulative.py
"""
import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os, time, requests, OpenDartReader
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')

d = OpenDartReader(os.getenv('DART_API_KEY'))
SB_URL = os.getenv('SB_URL')
SB_KEY = os.getenv('SB_SERVICE_KEY')
headers = {'apikey': SB_KEY, 'Authorization': f'Bearer {SB_KEY}'}

# 다양한 업종 샘플 — corp_code는 DB에서 조회
SAMPLE_NAMES = [
    '삼성전자', 'SK하이닉스', '현대차', 'LG화학', '삼성SDI',
    '셀트리온', '카카오', 'POSCO홀딩스', '두산에너빌리티', '한화솔루션',
    '삼성바이오로직스', 'KB금융', '신한지주', '하나금융지주', 'LG전자',
    '현대모비스', 'SK이노베이션', 'LG에너지솔루션', 'HD현대중공업', '한국전력',
]

# DB에서 corp_code 일괄 조회
names_str = ','.join([f'"{n}"' for n in SAMPLE_NAMES])
r = requests.get(f'{SB_URL}/rest/v1/companies',
    headers=headers,
    params={
        'name': f'in.({",".join(SAMPLE_NAMES)})',
        'select': 'name,corp_code',
        'limit': '50'
    })
corp_map = {row['name']: row['corp_code'] for row in r.json() if row.get('corp_code')}

REVENUE_ACCTS = ['매출액', '수익(매출액)', '영업수익']

def get_revenue(corp_code, year, reprt_code, fs_div='CFS'):
    try:
        df = d.finstate_all(corp_code, bsns_year=year, reprt_code=reprt_code, fs_div=fs_div)
        if df is None:
            return None
        for acct in REVENUE_ACCTS:
            row = df[df['account_nm'] == acct]
            if not row.empty:
                val = row['thstrm_amount'].values[0]
                try:
                    return int(str(val).replace(',', ''))
                except:
                    return None
        return None
    except:
        return None

print(f"\n{'='*100}")
print(f"{'종목명':<16} {'Q1(억)':>10} {'Q2보고서(억)':>13} {'Q3보고서(억)':>13} {'Q2-Q1(억)':>11} {'Q2판정':>8} {'Q3-Q2(억)':>11} {'Q3판정':>8}")
print(f"{'='*100}")

cumul_list = []
single_list = []
unknown_list = []

for name in SAMPLE_NAMES:
    corp_code = corp_map.get(name)
    if not corp_code:
        print(f"{name:<16} DB에 corp_code 없음")
        continue

    q1 = get_revenue(corp_code, '2024', '11013')
    time.sleep(0.2)
    q2 = get_revenue(corp_code, '2024', '11012')
    time.sleep(0.2)
    q3 = get_revenue(corp_code, '2024', '11014')
    time.sleep(0.2)

    def fmt(v):
        return f"{int(v)//int(1e8):,}" if v is not None else "—"

    if q1 is None or q2 is None:
        q2_judge = "확인불가"
        unknown_list.append(name)
    elif q2 > q1 * 1.8:
        # Q2가 Q1의 1.8배 이상이면 누적일 가능성 높음
        q2_judge = "누적❗"
        cumul_list.append(name)
    else:
        q2_judge = "단독✅"
        single_list.append(name)

    if q2 is None or q3 is None:
        q3_judge = "확인불가"
    elif q3 > (q2 or 0) * 1.8:
        q3_judge = "누적❗"
    else:
        q3_judge = "단독✅"

    diff_q2 = (q2 - q1) if (q1 and q2) else None
    diff_q3 = (q3 - q2) if (q2 and q3) else None

    print(f"{name:<16} {fmt(q1):>10} {fmt(q2):>13} {fmt(q3):>13} "
          f"{fmt(diff_q2):>11} {q2_judge:>8} {fmt(diff_q3):>11} {q3_judge:>8}")

print(f"\n{'='*100}")
print(f"  단독값 제공: {', '.join(single_list)}")
print(f"  누적값 의심: {', '.join(cumul_list)}")
print(f"  확인불가:   {', '.join(unknown_list)}")
print(f"{'='*100}\n")
