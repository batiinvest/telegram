# audit_parse.py — 공시 파싱 품질 전수 감사 (서버에서 실행)
# 최근 5거래일 상장사 공시를 유형별 샘플링 → get_disclosure_detail 실행 →
# 깨짐/누락/가독성 플래그 자동 판정 → /tmp/dart_audit.json 저장 + 요약 출력
import os, json, re, time, requests
from collections import defaultdict, Counter
from dotenv import load_dotenv

load_dotenv("/home/kjhofone/.env")
KEY = os.environ["DART_API_KEY"]

DATES = ["20260716", "20260715", "20260714", "20260713", "20260710"]

items = []
for date in DATES:
    for page in range(1, 40):
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": KEY, "bgn_de": date, "end_de": date,
                    "page_count": 100, "page_no": page}, timeout=10).json()
        if r.get("status") != "000":
            break
        items.extend(r.get("list", []))
        if page >= int(r.get("total_page", 1) or 1):
            break

# KRX 안내류는 괄호 내용이 곧 유형 — 괄호 유지, 그 외는 괄호 제거로 유형 병합
_KEEP_PAREN = ("기타시장안내", "투자유의안내", "기타경영사항", "풍문또는보도")


def type_sig(nm: str) -> str:
    s = re.sub(r"^\[[^\]]+\]", "", nm)
    s = re.sub(r"\s+", "", s).strip()
    if not s.startswith(_KEEP_PAREN):
        s = re.sub(r"\(.*?\)", "", s)
    return s[:40]


by_type = defaultdict(list)
for it in items:
    if not (it.get("stock_code") or "").strip():
        continue
    by_type[type_sig(it.get("report_nm", ""))].append(it)

sample = []
for sig, lst in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
    for it in lst[:2]:
        sample.append((sig, it))
sample = sample[:160]
print(f"수집 {len(items)}건 → 상장사 유형 {len(by_type)}종 → 샘플 {len(sample)}건")

from dart_parser import get_disclosure_detail, PARSER_STATS


def quality(out: str) -> list:
    if not out:
        return ["EMPTY"]
    flags = []
    if "�" in out or re.search(r"\?{2,}", out):
        flags.append("GARBLED")
    if "_html" in out or "_rcept" in out:
        flags.append("LEAK")
    if re.search(r"<[a-zA-Z/][^>]*>", out):
        flags.append("HTMLTAG")
    if re.search(r"&(cr|nbsp|amp|lt|gt|#\d+);", out):
        flags.append("ENTITY")
    lines = out.split("\n")
    if any(len(l) > 220 for l in lines):
        flags.append("LONGLINE")
    if len(out) > 1600:
        flags.append("LONGMSG")
    if len(lines) >= 14:
        flags.append("MANYLINES")
    return flags


results = []
for i, (sig, it) in enumerate(sample):
    rc, nm = it["rcept_no"], it["report_nm"].strip()
    PARSER_STATS.clear()
    try:
        out = get_disclosure_detail(rc, nm)
        err = ""
    except Exception as e:
        out, err = "", f"EXC:{type(e).__name__}"
    used = next(iter(PARSER_STATS), "?")
    results.append({"sig": sig, "rc": rc, "nm": nm, "parser": used,
                    "flags": quality(out) + ([err] if err else []),
                    "len": len(out), "out": out})
    time.sleep(0.1)

json.dump(results, open("/tmp/dart_audit.json", "w"), ensure_ascii=False, indent=1)

flag_counter = Counter()
parser_counter = Counter()
for r in results:
    parser_counter[r["parser"]] += 1
    for f in r["flags"]:
        flag_counter[f] += 1

print()
print("== 플래그 분포 ==")
for f, c in flag_counter.most_common():
    print(f"  {f}: {c}건")
print()
print("== 파서 사용 분포 ==")
for p, c in parser_counter.most_common():
    print(f"  {p}: {c}건")
print()
print("== 문제 항목 (EMPTY 제외 플래그) ==")
for r in results:
    bad = [f for f in r["flags"] if f != "EMPTY"]
    if bad:
        print(f"  [{','.join(bad)}] {r['parser']:<24} {r['nm'][:44]}")
print()
print("== EMPTY (상세 없음) 유형 ==")
empty_types = Counter(r["sig"] for r in results if "EMPTY" in r["flags"])
for sig, c in empty_types.most_common():
    print(f"  {sig} ({c})")
