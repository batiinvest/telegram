#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BatiInvest 데이터 신선도 자동 점검 (Task 7)
- 평일 19:00 cron 실행. logs/freshness.log 기록 + (ADMIN_CHAT_ID 설정 시) 텔레그램 알림.
- 기준 시계 = market_data 최신 base_date (=마지막 실제 거래일) → 주말/공휴일 자동 처리.
"""
import os, sys, datetime, requests
from dotenv import load_dotenv
load_dotenv('/home/kjhofone/.env')
from db_client import get_supabase_client

SB_URL = os.getenv('SB_URL'); SB_KEY = os.getenv('SB_SERVICE_KEY')
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', ''); ADMIN_CHAT = os.getenv('ADMIN_CHAT_ID', '')
LOG = '/home/kjhofone/logs/bati.log'
sb = get_supabase_client()
stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def cnt(table, **eq):
    q = sb.table(table).select('*', count='exact')
    for k, v in eq.items(): q = q.eq(k, v)
    return q.limit(1).execute().count or 0

def latest(table, col='base_date'):
    r = sb.table(table).select(col).order(col, desc=True).limit(1).execute()
    return ((r.data or [{}])[0].get(col) or '')[:10] if r.data else ''

issues = []; lines = []
ref = latest('market_data')
lines.append(f"기준 거래일(market_data 최신): {ref}")

# 1) 일별 신선도 (기준일과 동일해야)
for t, label in [('macro_data','매크로'),('sector_daily_summary','섹터요약'),
                 ('leading_stocks','주도주'),('daily_disclosures','공시')]:
    lt = latest(t)
    if lt != ref: issues.append(f"{label}({t}) 지연: 최신 {lt} (기준 {ref})")
    lines.append(f"  {label:<10} 최신={lt}")

um = latest('us_market')
if um and ref:
    d = (datetime.date.fromisoformat(ref) - datetime.date.fromisoformat(um)).days
    if d > 4: issues.append(f"US ETF(us_market) {d}일 지연 (최신 {um})")
lines.append(f"  US_ETF     최신={um}")

cu = latest('companies','updated_at')
if cu:
    d = (datetime.date.today() - datetime.date.fromisoformat(cu)).days
    if d > 14: issues.append(f"상장사(companies) updated_at {d}일 정체 (최신 {cu})")
lines.append(f"  상장사     updated_at={cu}")

# 2) 당일 건수 범위
lines.append("당일 건수:")
for t, label, mn, mx in [('market_data','시장데이터',2000,6000),
        ('short_selling_history','공매도',50,600),('sector_daily_summary','섹터요약',7,20),
        ('leading_stocks','주도주',30,60),('daily_disclosures','공시',5,800),('macro_data','매크로',1,5)]:
    c = cnt(t, base_date=ref); flag=''
    if c < mn: flag=' ⚠️적음'; issues.append(f"{label}({t}) 건수 {c} < {mn} ({ref})")
    elif c > mx: flag=' ⚠️많음'; issues.append(f"{label}({t}) 건수 {c} > {mx} ({ref})")
    lines.append(f"  {label:<10} {c}건 (기대 {mn}~{mx}){flag}")

# 3) 오늘 로그 ERROR 스캔
today = datetime.date.today().isoformat(); err=0; sigs={}
try:
    with open(LOG, encoding='utf-8', errors='ignore') as f:
        for ln in f:
            if ln.startswith(today) and '[ERROR]' in ln:
                err += 1
                k = 'gemini' if 'gemini' in ln.lower() else ('공매도' if '공매도' in ln else ln.split('] ',2)[-1][:35].strip())
                sigs[k] = sigs.get(k,0)+1
except Exception as e: lines.append(f"로그 읽기 오류: {e}")
lines.append(f"오늘 로그 ERROR: {err}건")
if err >= 20:
    top = sorted(sigs.items(), key=lambda x:-x[1])[:3]
    issues.append("로그 ERROR 급증 "+str(err)+"건 — "+", ".join(f"{k}×{v}" for k,v in top))

report = f"[{stamp}] 데이터점검 — 이상 {len(issues)}건\n" + "\n".join(lines)
print(report)
if issues:
    print("⚠️ ISSUES:")
    for i in issues: print("  - "+i)

if issues and TG_TOKEN and ADMIN_CHAT:
    m = f"🚨 <b>[BatiInvest 데이터 이상]</b> {today}\n" + "════════════" + "\n" + "\n".join("• "+i for i in issues)
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={'chat_id':ADMIN_CHAT,'text':m,'parse_mode':'HTML'}, timeout=15)
        print("텔레그램 알림 발송")
    except Exception as e: print(f"텔레그램 발송 실패: {e}")
elif issues:
    print("(텔레그램 알림 생략: ADMIN_CHAT_ID 미설정 — .env에 추가하면 활성화)")
sys.exit(0)
