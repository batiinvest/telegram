
from logger_config import get_logger
log = get_logger(__name__)
"""
collect_listed_companies.py
───────────────────────────
dart_fss로 코스피+코스닥 전체 상장사를 동기화합니다.

처리 케이스:
  1. 신규상장  — DB에 없는 종목 → monitoring_level='data'로 추가
  2. 사명변경  — 같은 code, 다른 name → DB name 업데이트 (중복 방지)
               단, full/news 종목은 이름 변경 시 경고 출력 (수동 확인 필요)
  3. 상장폐지  — DART 목록에서 사라진 종목 → data 레벨만 자동 삭제
               full/news 종목은 자동 삭제하지 않고 경고 출력
  4. 기존종목  — market, sector, product, corp_code 업데이트

실행:
    python3 collect_listed_companies.py
    python3 collect_listed_companies.py --dry-run  # 변경사항만 확인
"""

import os, sys, time, argparse
from dotenv import load_dotenv
load_dotenv()

try:
    import dart_fss as dart
except ImportError:
    print("pip install dart-fss 필요"); sys.exit(1)

try:
    from db_client import get_supabase_client as _get_sb
except ImportError:
    from supabase import create_client as _cs
    def _get_sb(): return _cs(os.getenv("SB_URL",""), os.getenv("SB_SERVICE_KEY",""))

from collect_utils import fetch_all_pages as _fetch_all_pages

DART_API_KEY   = os.getenv("DART_API_KEY", "")
SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")



def run(dry_run: bool = False):
    if not all([DART_API_KEY, SB_URL, SB_SERVICE_KEY]):
        # sys.exit 금지 — 스케줄러 잡에서 호출되므로 SystemExit가 except Exception을 뚫음
        raise RuntimeError("DART_API_KEY, SB_URL, SB_SERVICE_KEY 환경변수 필요")

    dart.set_api_key(DART_API_KEY)
    sb = _get_sb()
    mode = "[DRY-RUN] " if dry_run else ""
    log.info(f"=== {mode}코스피+코스닥 전체 상장사 동기화 시작 ===")

    # 1. DART 로드
    log.info("DART 상장사 목록 로드 중...")
    corp_list = dart.get_corp_list()
    listed    = corp_list.find_by_corp_name('', market='YK')
    log.info(f"DART: {len(listed)}개")

    dart_map = {}
    for corp in listed:
        code = (corp.stock_code or "").strip()
        if not code: continue
        cls = getattr(corp, 'corp_cls', '') or ''
        dart_map[code] = {
            "name":      (corp.corp_name or "").strip(),
            "corp_code": (corp.corp_code or "").strip(),
            "market":    'KOSPI' if cls == 'Y' else 'KOSDAQ' if cls == 'K' else '',
            "sector":    (getattr(corp, 'sector',  '') or '').strip(),
            "product":   (getattr(corp, 'product', '') or '').strip(),
        }

    # 2. DB 로드
    # ⚠️ chat_id는 rooms 테이블로 분리돼 companies에 없음 — select에 포함하면
    #   42703으로 조회 실패 → fetch_all_pages가 빈 리스트 반환 → 전체가 '신규' 오판
    #   → chat_id 포함 insert가 PGRST204로 전멸 (07-04~07-11 동기화 무동작의 원인)
    log.info("DB 기존 종목 로드 중...")
    rows = _fetch_all_pages(sb.table("companies").select(
        "id,name,code,corp_code,market,monitoring_level,is_monitored"
    ))
    db_map = {
        (row.get("code") or "").strip(): row
        for row in rows
        if (row.get("code") or "").strip()
    }
    log.info(f"DB: {len(db_map)}개")

    # 조회 실패(빈 결과)를 '전부 신규'로 오판하지 않도록 가드 —
    # DART가 수천 개인데 DB가 0개면 쿼리 실패 가능성이 압도적
    if not db_map:
        raise RuntimeError("companies 조회 결과 0건 — 쿼리 실패 의심, 동기화 중단 (오판 방지)")

    dart_codes = set(dart_map.keys())
    db_codes   = set(db_map.keys())

    # 3. 케이스 분류
    new_listings        = []
    name_changes        = []
    to_update           = []
    delisted_data       = []
    delisted_monitored  = []

    for code, di in dart_map.items():
        if code not in db_map:
            new_listings.append({
                "name": di["name"], "code": code,
                "corp_code": di["corp_code"], "market": di["market"],
                "sector": di["sector"], "product": di["product"],
                "industry": "", "sub_industry": "",
                "keywords": "",
                "active": True, "is_monitored": False, "monitoring_level": "data",
            })
        else:
            row = db_map[code]
            payload = {
                "corp_code": di["corp_code"],
                "market":    di["market"],
                "sector":    di["sector"],
                "product":   di["product"],
            }
            if row["name"] != di["name"]:
                payload["name"] = di["name"]
                name_changes.append({
                    "code": code, "old_name": row["name"],
                    "new_name": di["name"], "level": row["monitoring_level"],
                })
            to_update.append({"id": row["id"], "payload": payload})

    for code, row in db_map.items():
        if row.get("market") == "KONEX": continue
        if code not in dart_codes:
            level = row.get("monitoring_level", "data")
            if level == "data":
                delisted_data.append(row)
            else:
                delisted_monitored.append(row)

    # 4. 리포트
    log.info(f"\n{'='*50}")
    log.info(f"신규상장:        {len(new_listings)}개")
    log.info(f"사명변경:        {len(name_changes)}개")
    log.info(f"상폐(data):      {len(delisted_data)}개 → 자동 삭제")
    log.info(f"상폐(모니터링):  {len(delisted_monitored)}개 → 수동 확인 필요")

    if name_changes:
        log.info("\n[사명변경 목록]")
        for nc in name_changes:
            log.info(f"  {nc['old_name']} → {nc['new_name']} ({nc['code']}, {nc['level']})")

    if delisted_monitored:
        log.warning("\n[⚠️  상폐 경고 — 수동 처리 필요]")
        for d in delisted_monitored:
            log.warning(f"  {d['name']} ({d.get('code')}) level={d.get('monitoring_level')}")

    if dry_run:
        log.info("\n[DRY-RUN] DB 변경 없음. --dry-run 제거 후 재실행하세요.")
        return

    # 5. DB 반영
    inserted = 0
    for i in range(0, len(new_listings), 100):
        batch = new_listings[i:i+100]
        try:
            sb.table("companies").upsert(batch, on_conflict="name", ignore_duplicates=True).execute()
            inserted += len(batch)
            log.info(f"신규 저장: {inserted}/{len(new_listings)}개")
        except Exception as e:
            log.error(f"신규 저장 실패: {e}")
        time.sleep(0.2)

    updated = 0
    for item in to_update:
        try:
            sb.table("companies").update(item["payload"]).eq("id", item["id"]).execute()
            updated += 1
        except Exception as e:
            log.debug(f"업데이트 실패 id={item['id']}: {e}")
        time.sleep(0.03)
    log.info(f"업데이트: {updated}개 (사명변경 {len(name_changes)}개 포함)")

    deleted = 0
    for d in delisted_data:
        try:
            sb.table("companies").delete().eq("id", d["id"]).execute()
            deleted += 1
            log.info(f"상폐 삭제: {d['name']} ({d.get('code')})")
        except Exception as e:
            log.error(f"삭제 실패 {d['name']}: {e}")
        time.sleep(0.05)

    # 6. 최종 현황
    def cnt(col, val):
        return sb.table("companies").select("id", count="exact").eq(col, val).execute().count or 0

    total  = sb.table("companies").select("id", count="exact").execute().count or 0
    log.info(f"""
=== 동기화 완료 ===
신규추가:  {inserted}개
업데이트:  {updated}개
상폐삭제:  {deleted}개 (data 레벨만)
상폐경고:  {len(delisted_monitored)}개 (수동 처리 필요)

총 종목수: {total}개
  full:    {cnt('monitoring_level','full')}개
  news:    {cnt('monitoring_level','news')}개
  data:    {cnt('monitoring_level','data')}개
코스피:    {cnt('market','KOSPI')}개
코스닥:    {cnt('market','KOSDAQ')}개
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="코스피+코스닥 전체 상장사 동기화")
    parser.add_argument("--dry-run", action="store_true", help="변경사항만 확인")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
