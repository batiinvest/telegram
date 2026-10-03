"""
4분기 누적 행(=연간값)과 섞여 계산된 저장 성장률을 None으로 정리 (2026-10-03 일회성).

financials의 4분기 누적 행(is_cumulative=true)은 손익이 사업보고서 연간값이다(fin_rules 참고).
수집기가 이를 분기 단독과 섞어 비교해 다음 해 1분기 QoQ에 '거짓 급감' 등이 저장됐다.
수집기는 4e03442·1bc559b로 고쳤고, 이 스크립트는 이미 저장된 값만 정리한다.

대상 — 연간값과 분기값이 섞인 비교만:
  ① 4분기 누적 행: QoQ 전부(직전 3분기와 비교 불가) / YoY는 전년 4분기가 분기값일 때만
     (전년 4분기도 누적이면 연간↔연간 비교라 정상 — 리츠 등은 유지)
  ② 다음 해 1분기(같은 종목·fs_div): QoQ (직전 분기 = 연간값)
  ③ 다음 해 4분기(같은 종목·fs_div, 그 자신은 분기값): YoY (전년 동기 = 연간값)

안전장치: 지울 칸마다 배포된 collect_financials.calculate_growth_rates로 재계산해
None이 나오는지 대조 — 하나라도 어긋나면 적용 중단.

사용 (서버 /home/kjhofone):
  python3 scripts/backfill_growth_annual_q4.py           # dry-run
  python3 scripts/backfill_growth_annual_q4.py --apply   # backups/에 원래값 저장 후 적용
되돌리기: 백업 JSON의 id별 원래값으로 update.
"""
import os as _os
import sys
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import datetime
from collections import Counter

from db_client import get_supabase_client
from db_utils import fetch_all_pages
from collect_utils import safe_execute
import collect_financials as CF

YOY = ["revenue_yoy", "op_profit_yoy", "net_income_yoy"]
QOQ = ["revenue_qoq", "op_profit_qoq", "net_income_qoq"]
VALS = ["revenue", "operating_profit", "net_income"]
SEL = "id,stock_code,corp_name,bsns_year,quarter,fs_div,is_cumulative," + ",".join(VALS + YOY + QOQ)
APPLY = "--apply" in sys.argv

sb = get_supabase_client()

q4c = fetch_all_pages(sb.table("financials").select(SEL)
                      .eq("is_cumulative", True).eq("quarter", "Q4"))
codes = sorted({r["stock_code"] for r in q4c})
rows = []
for i in range(0, len(codes), 100):
    rows += fetch_all_pages(sb.table("financials").select(SEL).in_("stock_code", codes[i:i + 100]))
idx = {(r["stock_code"], r["bsns_year"], r["quarter"], r["fs_div"]): r for r in rows}
print(f"4분기 누적 행 {len(q4c)} / 관련 종목 {len(codes)} / 조회 행 {len(rows)}")

plan = {}   # id → {"row", "null": set(필드), "why": set()}


def add(row, fields, why):
    cur = [f for f in fields if row.get(f) is not None]
    if cur:
        p = plan.setdefault(row["id"], {"row": row, "null": set(), "why": set()})
        p["null"].update(cur)
        p["why"].add(why)


kept_annual_yoy = 0
for r in q4c:
    code, y, fs = r["stock_code"], int(r["bsns_year"]), r["fs_div"]
    add(r, QOQ, "①4Q누적 QoQ")
    prev = idx.get((code, str(y - 1), "Q4", fs))
    if prev and prev.get("is_cumulative"):
        kept_annual_yoy += sum(1 for f in YOY if r.get(f) is not None)
    else:
        add(r, YOY, "①4Q누적 YoY(연간↔분기)")
    q1 = idx.get((code, str(y + 1), "Q1", fs))
    if q1:
        add(q1, QOQ, "②다음해 1Q QoQ")
    q4 = idx.get((code, str(y + 1), "Q4", fs))
    if q4 and not q4.get("is_cumulative"):
        add(q4, YOY, "③다음해 4Q YoY")

# ── 안전장치: 배포된 수집기 규칙으로 재계산해 대상 칸이 None인지 대조 ──
cache = {}
for r in rows:
    cache.setdefault(r["stock_code"], {})[(r["bsns_year"], r["quarter"], r["fs_div"])] = CF._cache_entry(r)
mismatch = []
for p in plan.values():
    g = CF.calculate_growth_rates(cache, p["row"])
    bad = [f for f in p["null"] if g.get(f) is not None]
    if bad:
        mismatch.append((p["row"]["corp_name"], p["row"]["bsns_year"], p["row"]["quarter"], bad))

why = Counter(w for p in plan.values() for w in p["why"])
fields = Counter(f for p in plan.values() for f in p["null"])
print(f"수정 대상 행 {len(plan)} — 사유별 {dict(why)}")
print(f"None으로 바꿀 칸 {sum(fields.values())} — {dict(fields)}")
print(f"유지(연간↔연간 YoY) 칸 {kept_annual_yoy}")
print(f"수집기 규칙 대조: 불일치 {len(mismatch)}건", mismatch[:5])
for p in list(plan.values())[:5]:
    r = p["row"]
    print(f"  예: {r['corp_name']} {r['bsns_year']}{r['quarter']} {r['fs_div']} {sorted(p['why'])} → "
          + ", ".join(f"{f}={r[f]}" for f in sorted(p["null"])))

if mismatch:
    print("\n❌ 수집기 규칙과 어긋나는 대상이 있어 중단")
    sys.exit(1)
if not APPLY:
    print("\n[dry-run] 변경 없음. 적용하려면 --apply")
    sys.exit(0)

stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
bdir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "backups")
_os.makedirs(bdir, exist_ok=True)
path = _os.path.join(bdir, f"financials_growth_backup_{stamp}.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump([{"id": i, "nulled": sorted(p["null"]), **{k: p["row"].get(k) for k in YOY + QOQ}}
               for i, p in plan.items()], f, ensure_ascii=False)
print(f"백업 저장: {path} ({len(plan)}행)")

ok = fail = 0
for i, p in plan.items():
    try:
        safe_execute(sb.table("financials").update({k: None for k in p["null"]}).eq("id", i),
                     retries=3, base_sleep=1.0, label="growth-backfill")
        ok += 1
    except Exception as e:
        fail += 1
        print("  실패:", i, e)
print(f"적용 완료: 성공 {ok} / 실패 {fail}")
