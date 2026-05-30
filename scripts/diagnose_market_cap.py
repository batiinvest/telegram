"""
diagnose_market_cap.py
──────────────────────
특정 종목의 market_cap 수집 문제 진단 스크립트

사용법:
    python diagnose_market_cap.py 001820
    python diagnose_market_cap.py 001820 삼화콘덴서공업
"""
import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import sys
import os
import json
from dotenv import load_dotenv
load_dotenv()

from managers import kis_auth, safe_int, safe_float
import stock_api
from supabase import create_client

SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")


def diagnose(code: str, name: str = ""):
    code = code.strip().split(".")[0]
    name = name or code
    print(f"\n{'='*60}")
    print(f"  진단 대상: {name} ({code})")
    print(f"{'='*60}\n")

    # ──────────────────────────────────────────────
    # 1. KIS API raw 응답 확인
    # ──────────────────────────────────────────────
    print("[ 1 ] KIS FHKST01010100 (inquire-price) raw 응답")
    print("-"*50)
    raw = stock_api.get_raw_price(code)
    if not raw:
        print("  ❌ API 응답 없음 (토큰 오류 or 잘못된 종목코드)")
        return

    output = raw.get("output", raw)

    # 핵심 필드만 출력
    key_fields = {
        "hts_avls":               "시가총액(억) → market_cap",
        "lstn_stcn":              "상장주식수 → listing_shares",
        "stck_prpr":              "현재가 → price",
        "new_hgpr_lwpr_cls_code": "신고가/신저가 구분 → hgpr_cls_code",
        "rprs_mrkt_kor_name":     "시장",
        "per":                    "PER",
        "pbr":                    "PBR",
    }
    for field, desc in key_fields.items():
        val = output.get(field)
        print(f"  {field:35s} = {repr(val):20s}  ({desc})")

    print()

    # 계산 시뮬레이션
    hts_avls  = safe_int(output.get("hts_avls"),  zero_as_none=True)
    lstn_stcn = safe_int(output.get("lstn_stcn"), zero_as_none=True)
    stck_prpr = safe_int(output.get("stck_prpr"), zero_as_none=True)

    if hts_avls:
        market_cap = hts_avls * 100_000_000
        print(f"  ✅ market_cap (hts_avls 기준): {market_cap:,}원 ({hts_avls:,}억)")
    elif lstn_stcn and stck_prpr:
        market_cap = lstn_stcn * stck_prpr
        print(f"  ⚠️  hts_avls=0 → fallback: {stck_prpr:,}원 × {lstn_stcn:,}주 = {market_cap:,}원")
    else:
        market_cap = None
        print(f"  ❌ market_cap 계산 불가")
        print(f"     hts_avls  = {output.get('hts_avls')!r}")
        print(f"     lstn_stcn = {output.get('lstn_stcn')!r}")
        print(f"     stck_prpr = {output.get('stck_prpr')!r}")

    # ──────────────────────────────────────────────
    # 2. Supabase DB 현황 확인
    # ──────────────────────────────────────────────
    print(f"\n[ 2 ] Supabase DB market_data 현황")
    print("-"*50)
    if not SB_URL or not SB_SERVICE_KEY:
        print("  ❌ SB_URL / SB_SERVICE_KEY 환경변수 없음")
        return

    sb = create_client(SB_URL, SB_SERVICE_KEY)

    # 최근 10일 데이터
    res = sb.table("market_data") \
            .select("base_date,price,market_cap,listing_shares,hgpr_cls_code") \
            .eq("stock_code", code) \
            .order("base_date", desc=True) \
            .limit(10) \
            .execute()

    rows = res.data or []
    if not rows:
        print(f"  ❌ market_data에 {code} 레코드 없음")
    else:
        print(f"  {'base_date':12s}  {'price':>10s}  {'market_cap':>18s}  {'listing_shares':>15s}  hgpr_cls_code")
        for r in rows:
            cap = f"{r['market_cap']:,}" if r.get('market_cap') else "NULL"
            shares = f"{r['listing_shares']:,}" if r.get('listing_shares') else "NULL"
            price = f"{r['price']:,}" if r.get('price') else "NULL"
            hgpr  = r.get('hgpr_cls_code') or "-"
            print(f"  {r['base_date']:12s}  {price:>10s}  {cap:>18s}  {shares:>15s}  {hgpr}")

    # ──────────────────────────────────────────────
    # 3. companies 테이블 확인
    # ──────────────────────────────────────────────
    print(f"\n[ 3 ] companies 테이블 현황")
    print("-"*50)
    res2 = sb.table("companies") \
             .select("code,name,is_monitored,active,industry") \
             .or_(f"code.eq.{code}.KS,code.eq.{code}.KQ,code.eq.{code}") \
             .execute()

    comp_rows = res2.data or []
    if not comp_rows:
        print(f"  ❌ companies에 {code} 없음 → collect_market 수집 안 됨!")
    else:
        for c in comp_rows:
            print(f"  code={c.get('code'):15s}  name={c.get('name'):20s}  "
                  f"is_monitored={c.get('is_monitored')}  active={c.get('active')}  "
                  f"industry={c.get('industry')}")

    # ──────────────────────────────────────────────
    # 4. 결론
    # ──────────────────────────────────────────────
    print(f"\n[ 4 ] 진단 결론")
    print("-"*50)
    has_cap_in_db = any(r.get('market_cap') for r in rows)
    in_companies  = bool(comp_rows)
    is_active     = any(c.get('active') for c in comp_rows)
    is_monitored  = any(c.get('is_monitored') for c in comp_rows)

    if not in_companies:
        print("  🔴 원인: companies 테이블에 없음 → all_listed 수집 대상 아님")
        print("     해결: Supabase에서 companies 테이블에 이 종목 추가 필요")
    elif not is_active:
        print("  🔴 원인: companies.active = False → all_listed 수집 제외됨")
        print("     해결: Supabase에서 active = true 로 변경")
    elif market_cap is None:
        print("  🔴 원인: KIS API가 hts_avls=0, lstn_stcn=0 반환 → market_cap 계산 불가")
        print("     해결: 아래 직접 계산값을 DB에 수동 입력하거나,")
        print("           KIS API 다른 엔드포인트(FHKST01010200 등) 확인 필요")
    elif not has_cap_in_db:
        print("  🟡 원인: KIS API는 market_cap 반환하지만 DB에 저장 안 됨")
        print("     → 다음 전체수집(17:00) 후 다시 확인")
    else:
        print("  ✅ DB에 market_cap 있음 — JS 쿼리 문제일 수 있음")
        best = next((r['market_cap'] for r in rows if r.get('market_cap')), None)
        print(f"     최근 market_cap: {best:,}원")

    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("사용법: python diagnose_market_cap.py <종목코드> [종목명]")
        print("예시:   python diagnose_market_cap.py 001820 삼화콘덴서공업")
        sys.exit(1)

    stock_code = args[0]
    stock_name = args[1] if len(args) > 1 else ""
    diagnose(stock_code, stock_name)
