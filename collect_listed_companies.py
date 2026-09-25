
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
  3. 상장폐지  — DART 목록에서 사라진 종목 → 전부 경고만(자동삭제 폐지)
               ※ 거래정지도 제외돼 상폐와 구분불가 → 전부 미삭제·수동확인
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

from collect_utils import fetch_all_pages as _fetch_all_pages, batch_upsert
from kis_client import get_raw_price as _get_raw_price   # 상폐 2차 판정용
import io, zipfile, re
import requests

DART_API_KEY   = os.getenv("DART_API_KEY", "")
SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")



# ── KIS 종목 마스터 (KRX kind.krx Akamai 차단 대체, 2026-08) ──
_KIS_MASTER = {
    "KOSPI":  "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}
_KIS_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


def _is_spac(name: str) -> bool:
    """스팩 판별 — 종목명 기준. 대부분 '스팩'이지만 정식명인 '기업인수목적'을
    그대로 쓴 것도 있다(예: 대신밸런스제17호기업인수목적). 2026-09 실측 72건 전수 커버.
    창업투자 '업종'으로 판별하면 에이티넘·스틱 같은 실제 VC 운용사까지 걸리므로 이름으로 본다."""
    n = name or ''
    return '스팩' in n or '기업인수목적' in n

def _load_kis_listed() -> dict:
    """KIS 종목 마스터에서 현재 매매가능 상장종목 로드.
    반환 {단축코드: {"name": 종목약명, "market": "KOSPI"|"KOSDAQ"}}.
    라인 포맷: [단축코드(9)+표준코드(12)+한글약명(가변)] + 고정 228바이트 부가정보.
    단축코드는 신형 IPO/스팩에서 문자 포함(예: 0126Z0) → 영숫자 6자리 허용.
    ⚠️ 매매가능 마스터라 거래정지 종목은 빠짐(상폐 판정 유의). ETF/ETN도 포함되나
       호출부에서 OpenDART corp_code 유무로 법인만 걸러낸다."""
    out = {}
    for market, url in _KIS_MASTER.items():
        last = None
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                break
            except Exception as e:
                last = e
                time.sleep(1.5)
        else:
            raise RuntimeError(f"KIS 마스터 다운로드 실패 ({market}): {last}")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        raw = zf.read(zf.namelist()[0]).decode("cp949", errors="replace").splitlines()
        for row in raw:
            if len(row) < 30:
                continue
            head = row[:len(row) - 228]
            code = head[0:9].strip()
            name = head[21:].strip()
            if _KIS_CODE_RE.match(code):
                out[code] = {"name": name, "market": market}
    return out


def _is_delisted(code: str) -> bool:
    """마스터 부재 종목이 정말 상장폐지인지 시세로 2차 확인한다.

    마스터 부재만으로는 단정할 수 없다 — 실측(2026-09-25) 거래량이 끊긴 118종목 중
    110종목이 매매가능 마스터에 그대로 있었고, 정리매매 4종목도 전부 잔존했다.
    즉 거래정지는 마스터에서 빠지지 않는다. 반대로 상폐 종목은 inquire-price가
    rt_cd=0을 주면서 현재가·상장주수를 0으로 채워 보낸다(= 코드 미존재 신호).
    조회 자체가 실패하면 판단하지 않는다 — 오판보다 미처리가 낫다.
    """
    try:
        raw = _get_raw_price(code)
        if not raw:
            return False
        o = raw.get("output", raw)
        px = str(o.get("stck_prpr") or "0").strip()
        sh = str(o.get("lstn_stcn") or "0").strip()
        return px in ("", "0") and sh in ("", "0")
    except Exception as e:
        log.debug(f"[상폐판정] {code} 시세 조회 실패 → 보류: {e}")
        return False


def run(dry_run: bool = False):
    if not all([DART_API_KEY, SB_URL, SB_SERVICE_KEY]):
        # sys.exit 금지 — 스케줄러 잡에서 호출되므로 SystemExit가 except Exception을 뚫음
        raise RuntimeError("DART_API_KEY, SB_URL, SB_SERVICE_KEY 환경변수 필요")

    dart.set_api_key(DART_API_KEY)
    sb = _get_sb()
    mode = "[DRY-RUN] " if dry_run else ""
    log.info(f"=== {mode}코스피+코스닥 전체 상장사 동기화 시작 ===")

    # 1. 상장 유니버스 — KIS 종목 마스터(현재 매매가능 상장) ∩ OpenDART corp_code
    #    (구: dart_fss find_by_corp_name(market=)가 kind.krx 스크래이프 의존 →
    #     KRX Akamai가 서버 데이터센터 IP 403 차단 → None·크래시. 2026-08 교체.)
    log.info("KIS 종목 마스터 로드 중...")
    kis_map = _load_kis_listed()
    log.info(f"KIS 마스터: {len(kis_map)}개 (매매가능 상장, ETF/ETN 포함)")

    log.info("OpenDART corp_code 매핑 로드 중...")
    corp_list = dart.get_corp_list()
    dart_corp = {}
    for corp in corp_list.corps:
        sc = (getattr(corp, "stock_code", "") or "").strip()
        if sc:
            dart_corp[sc] = ((corp.corp_code or "").strip(), (corp.corp_name or "").strip())

    # ETF/ETN/ELW는 법인 corp_code가 없어 교집합에서 자연 제외.
    # sector/product는 kind.krx 차단으로 소스 없음 → 기존 DB값 보존(to_update 미포함).
    dart_map = {}
    for code, info in kis_map.items():
        cc = dart_corp.get(code)
        if not cc:
            continue
        corp_code, corp_name = cc
        dart_map[code] = {
            "name":      corp_name or info["name"],
            "corp_code": corp_code,
            "market":    info["market"],
        }
    log.info(f"상장사(법인) 확정: {len(dart_map)}개")

    # 조회 실패로 전량 상폐 오판 방지 — 정상시 KOSPI+KOSDAQ ~2650개
    if len(dart_map) < 2000:
        raise RuntimeError(f"상장사 목록 비정상 ({len(dart_map)}개) — KIS/OpenDART 로드 실패 의심, 중단")

    # 2. DB 로드
    # ⚠️ chat_id는 rooms 테이블로 분리돼 companies에 없음 — select에 포함하면
    #   42703으로 조회 실패 → fetch_all_pages가 빈 리스트 반환 → 전체가 '신규' 오판
    #   → chat_id 포함 insert가 PGRST204로 전멸 (07-04~07-11 동기화 무동작의 원인)
    log.info("DB 기존 종목 로드 중...")
    rows = _fetch_all_pages(sb.table("companies").select(
        "id,name,code,corp_code,market,monitoring_level,is_monitored,active"
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

    # 3. 케이스 분류
    new_listings        = []
    name_changes        = []
    to_update           = []
    delisted_data       = []
    delisted_monitored  = []

    spac_skipped = 0
    for code, di in dart_map.items():
        if code not in db_map:
            # 스팩(SPAC)은 인수 대상을 찾기 전까지 사업도 재무도 없는 껍데기라
            # 시세·재무·업종 어느 수집에도 의미가 없다 → 애초에 담지 않는다.
            # (기존 재무 수집기들이 각자 '스팩' 이름으로 걸러내던 것을 유입 단계로 앞당김)
            if _is_spac(di["name"]):
                spac_skipped += 1
                continue
            new_listings.append({
                "name": di["name"], "code": code,
                "corp_code": di["corp_code"], "market": di["market"],
                "sector": "", "product": "",
                "industry": "", "sub_industry": "",
                "keywords": "",
                "active": True, "is_monitored": False, "monitoring_level": "data",
            })
        else:
            row = db_map[code]
            payload = {
                "corp_code": di["corp_code"],
                "market":    di["market"],
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
        # 이미 상폐 처리된 종목은 매주 다시 후보로 잡히지 않게 건너뛴다
        if row.get("active") is False: continue
        if code not in dart_codes:
            level = row.get("monitoring_level", "data")
            if level == "data":
                delisted_data.append(row)
            else:
                delisted_monitored.append(row)

    # 4. 리포트
    log.info(f"\n{'='*50}")
    log.info(f"신규상장:        {len(new_listings)}개")
    log.info(f"스팩 제외:       {spac_skipped}개")
    log.info(f"사명변경:        {len(name_changes)}개")
    log.info(f"상폐후보(data):  {len(delisted_data)}개 → 시세 2차 판정 예정")
    log.info(f"상폐후보(모니터링): {len(delisted_monitored)}개 → 시세 2차 판정 예정")

    if name_changes:
        log.info("\n[사명변경 목록]")
        for nc in name_changes:
            log.info(f"  {nc['old_name']} → {nc['new_name']} ({nc['code']}, {nc['level']})")

    if delisted_monitored:
        log.warning("\n[⚠️  모니터링 종목이 마스터 부재 — 시세 2차 판정 대상]")
        for d in delisted_monitored:
            log.warning(f"  {d['name']} ({d.get('code')}) level={d.get('monitoring_level')}")

    if dry_run:
        log.info("\n[DRY-RUN] DB 변경 없음. --dry-run 제거 후 재실행하세요.")
        return

    # 5. DB 반영
    inserted = batch_upsert(sb, "companies", new_listings, "name", chunk=100,
                            progress_label="신규 저장", ignore_duplicates=True, sleep=0.2)

    updated = 0
    for item in to_update:
        try:
            sb.table("companies").update(item["payload"]).eq("id", item["id"]).execute()
            updated += 1
        except Exception as e:
            log.debug(f"업데이트 실패 id={item['id']}: {e}")
        time.sleep(0.03)
    log.info(f"업데이트: {updated}개 (사명변경 {len(name_changes)}개 포함)")

    # 상폐 후보 2차 판정 — 시세가 살아 있으면 거래정지, 비어 있으면 상폐.
    # 구현 이전엔 '마스터 부재만으로는 거래정지와 구분 불가'로 보고 경고만 냈는데,
    # 수동 확인이 실제로는 이뤄지지 않아 상폐 8종목이 2026-09까지 active로 남아
    # 매일 price=NULL 행을 쌓고 표에 빈 줄로 노출됐다. 판정 근거는 _is_delisted 참조.
    # 삭제가 아니라 active=False이므로 행도 과거 market_data도 남는다 — 오판해도 되돌린다.
    MAX_AUTO_DEACTIVATE = 30   # 후보가 이보다 많으면 마스터 로드 이상을 의심해 손대지 않는다
    candidates = delisted_data + delisted_monitored
    deactivated, still_listed = [], []
    if len(candidates) > MAX_AUTO_DEACTIVATE:
        log.warning(f"[상폐] 후보 {len(candidates)}개 — {MAX_AUTO_DEACTIVATE}개 초과라 "
                    f"자동 비활성화를 건너뛴다(마스터 로드 이상 의심). 경고만 남긴다.")
        still_listed = candidates
    else:
        for d in candidates:
            code = (d.get("code") or "").split(".")[0]
            if not code:
                continue
            if not _is_delisted(code):
                still_listed.append(d)
                continue
            try:
                sb.table("companies").update({"active": False, "is_monitored": False})                   .eq("id", d["id"]).execute()
                deactivated.append(d)
            except Exception as e:
                log.warning(f"[상폐] {d['name']}({code}) 비활성화 실패: {e}")
            time.sleep(0.15)

    if deactivated:
        log.warning(f"[상폐] 비활성화 {len(deactivated)}개 — active=False (행·과거 시세는 보존)")
        for d in deactivated:
            log.warning(f"  {d['name']} ({d.get('code')}) level={d.get('monitoring_level')}")
    if still_listed:
        log.info(f"[상폐후보] {len(still_listed)}개는 시세가 살아 있어 거래정지로 보고 유지")
        for d in still_listed:
            log.info(f"  {d['name']} ({d.get('code')})")

    # 6. 최종 현황
    def cnt(col, val):
        return sb.table("companies").select("id", count="exact").eq(col, val).execute().count or 0

    total  = sb.table("companies").select("id", count="exact").execute().count or 0
    log.info(f"""
=== 동기화 완료 ===
신규추가:  {inserted}개
업데이트:  {updated}개
상폐처리:  {len(deactivated)}개 (active=False — 시세 빈응답 확인)
거래정지:  {len(still_listed)}개 (시세 살아있어 유지)

총 종목수: {total}개
  full:    {cnt('monitoring_level','full')}개
  news:    {cnt('monitoring_level','news')}개
  data:    {cnt('monitoring_level','data')}개
코스피:    {cnt('market','KOSPI')}개
코스닥:    {cnt('market','KOSDAQ')}개
""")

    # 호출부(jobs_collect)가 운영 알림에 실어 보낼 수 있게 결과를 돌려준다
    return {
        "inserted":    inserted,
        "updated":     updated,
        "deactivated": [(d["name"], (d.get("code") or "")) for d in deactivated],
        "still_listed": len(still_listed),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="코스피+코스닥 전체 상장사 동기화")
    parser.add_argument("--dry-run", action="store_true", help="변경사항만 확인")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
