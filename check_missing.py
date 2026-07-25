import sys, OpenDartReader, requests
sys.path.insert(0, '/home/kjhofone')

# .env 직접 로드
env = {}
with open('/home/kjhofone/.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

d = OpenDartReader(env['DART_API_KEY'])
SB_URL = env['SB_URL']
SB_KEY = env['SB_SERVICE_KEY']
headers = {'apikey': SB_KEY, 'Authorization': f'Bearer {SB_KEY}'}

corps = {'피에스케이': None, '에코프로비엠': None, '제이엘케이': None, '디어유': None}

for name in corps:
    r = requests.get(f'{SB_URL}/rest/v1/companies',
        headers=headers,
        params={'name': f'eq.{name}', 'select': 'name,corp_code'})
    data = r.json()
    if data:
        corps[name] = data[0]['corp_code']

for name, corp_code in corps.items():
    if not corp_code: continue
    print(f"\n{'='*40}\n{name} ({corp_code})")
    for reprt, label in [('11013','Q1'),('11012','Q2'),('11014','Q3'),('11011','Q4')]:
        df = d.finstate_all(corp_code, bsns_year='2024', reprt_code=reprt, fs_div='CFS')
        if df is None: continue
        mask = df['account_nm'].str.contains('순이익|순손익|순손실|순수익|순이이익', na=False)
        rows = df[mask][['account_nm','thstrm_amount']].drop_duplicates('account_nm')
        if not rows.empty:
            print(f"2024 {label}:")
            print(rows.to_string())
        break
