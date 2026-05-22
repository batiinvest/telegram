from dotenv import load_dotenv; load_dotenv()
from supabase_bridge import bridge as b
from collections import defaultdict
sb = b._get_client()

mon = sb.table('companies').select('code,name').eq('is_monitored', True).execute()
mon_codes = [r['code'].split('.')[0] for r in mon.data]
mon_names = {r['code'].split('.')[0]: r['name'] for r in mon.data}
print(f"모니터링 종목: {len(mon_codes)}개\n")

# 1. market_data
print("=" * 50)
print("📊 market_data (일별 시장 데이터)")
date_cnt = defaultdict(int)
from_idx = 0
while True:
    r = sb.table('market_data').select('base_date,stock_code').range(from_idx, from_idx+999).execute()
    if not r.data: break
    for row in r.data:
        date_cnt[row['base_date']] += 1
    if len(r.data) < 1000: break
    from_idx += 1000

dates = sorted(date_cnt.keys())
latest = dates[-1]
print(f"  기간: {dates[0]} ~ {dates[-1]}")
print(f"  날짜 수: {len(dates)}개 거래일")
print(f"  최소 종목수: {min(date_cnt.values())}개 / 최대: {max(date_cnt.values())}개")
r2 = sb.table('market_data').select('stock_code').eq('base_date', latest).in_('stock_code', mon_codes[:200]).execute()
print(f"  최신일({latest}) 모니터링 커버: {len(r2.data)}/{len(mon_codes)}개")

# 2. financials
print("\n" + "=" * 50)
print("💰 financials (재무 데이터)")
r3 = sb.table('financials').select('stock_code,year,quarter').limit(3000).execute()
if r3.data:
    years = sorted(set(row['year'] for row in r3.data))
    quarters = sorted(set(f"{row['year']}Q{row['quarter']}" for row in r3.data))
    stocks_fin = set(row['stock_code'] for row in r3.data)
    missing = [c for c in mon_codes if c not in stocks_fin]
    print(f"  연도 범위: {years[0]} ~ {years[-1]}")
    print(f"  최근 분기: {quarters[-3:]}")
    print(f"  재무 있는 종목: {len(stocks_fin)}개")
    print(f"  모니터링 중 재무 없는 종목: {len(missing)}개")
    if missing[:5]: print(f"  샘플: {[mon_names.get(c,c) for c in missing[:5]]}")
else:
    print("  데이터 없음")

# 3. company_info
print("\n" + "=" * 50)
print("🏢 company_info (기업 개황)")
r4 = sb.table('company_info').select('stock_code').limit(1000).execute()
if r4.data:
    ci_codes = set(row['stock_code'] for row in r4.data)
    missing_ci = [c for c in mon_codes if c not in ci_codes]
    print(f"  기업정보 있는 종목: {len(ci_codes)}개")
    print(f"  모니터링 중 기업정보 없는 종목: {len(missing_ci)}개")
    if missing_ci[:5]: print(f"  샘플: {[mon_names.get(c,c) for c in missing_ci[:5]]}")
else:
    print("  데이터 없음")

# 4. macro_data
print("\n" + "=" * 50)
print("🌍 macro_data (글로벌 지표)")
r5l = sb.table('macro_data').select('base_date,sp500,kospi,bitcoin,usd_krw').order('base_date', desc=True).limit(1).execute()
r5o = sb.table('macro_data').select('base_date').order('base_date', desc=False).limit(1).execute()
if r5l.data:
    m = r5l.data[0]
    print(f"  기간: {r5o.data[0]['base_date']} ~ {m['base_date']}")
    print(f"  최신: SP500={m['sp500']}, KOSPI={m['kospi']}, BTC={m['bitcoin']}, USD={m['usd_krw']}")

# 5. watchlist
print("\n" + "=" * 50)
print("📋 watchlist (투자 노트)")
r6 = sb.table('watchlist').select('stock_code').execute()
print(f"  투자 노트 종목: {len(r6.data)}개")
print("\n✅ 점검 완료")
