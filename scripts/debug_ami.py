import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import sys, os
sys.path.insert(0, '/home/kjhofone')
with open('/home/kjhofone/.env') as f:
    env = {k.strip():v.strip().strip('"') for line in f if '=' in line and not line.startswith('#') for k,v in [line.split('=',1)]}

os.environ.update(env)

import OpenDartReader as ODR
from collect_financials import resolve_account, parse_amount, FLOW_COLS

dart = ODR(env['DART_API_KEY'])
df = dart.finstate_all('00545734', bsns_year='2025', reprt_code='11011', fs_div='CFS')

print("=== Q4 사업보고서 전체 손익 계정 매핑 ===")
result = {}
for _, r in df.iterrows():
    acct = str(r.get('account_nm','')).strip()
    col = resolve_account(acct)
    if col and col in FLOW_COLS:
        amt = parse_amount(str(r.get('thstrm_amount','')))
        status = "✅ 저장" if col not in result else "⛔ 스킵(이미있음)"
        if col not in result and amt is not None:
            result[col] = amt
        print(f"  {status} '{acct}' → {col} = {amt//int(1e8) if amt else None}억")

print(f"\n최종 result['revenue'] = {result.get('revenue','없음')}")
