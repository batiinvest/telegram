"""
collect_investor_market.py
──────────────────────────
KIS 시장별 투자자매매동향(일별) → market_investor_flow 저장

수집 데이터 (코스피·코스닥, 일별, 단위: 백만원):
  {kospi,kosdaq}_{indi,frgn,orgn} : 개인/외국인/기관 순매수 대금 (양수=순매수)

KIS API (FHPTJ04040000, 2026-07 실측):
  GET /uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market
  코스피 = FID_INPUT_ISCD 0001 + FID_INPUT_ISCD_1 KSP / 코스닥 = 1001 + KSQ
  FID_COND_MRKT_DIV_CODE=U, custtype=P. 1콜당 조회일 이전 300거래일 반환(날짜 페이지네이션).
  *_ntby_tr_pbmn 단위=백만원(개인+외국인+기관+기타 합≈0 검증). 당일분은 장 마감 후 확정.

실행:
  python3 collect_investor_market.py                     # 최근 14일 (일일 잡)
  python3 collect_investor_market.py --backfill 20240101 # 지정일부터 백필
"""
import sys
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from managers import kis_auth

log = get_logger(__name__)

MARKETS = (('kospi', '0001', 'KSP'), ('kosdaq', '1001', 'KSQ'))
FIELDS = (('indi', 'prsn_ntby_tr_pbmn'),
          ('frgn', 'frgn_ntby_tr_pbmn'),
          ('orgn', 'orgn_ntby_tr_pbmn'))


def _fetch_window(end_yyyymmdd: str) -> dict:
    """end일 기준 양 시장 각 1콜(300거래일) 조회 → {base_date(iso): 병합행}"""
    merged = {}
    for name, iscd, iscd1 in MARKETS:
        data = kis_auth.kis_get(
            "FHPTJ04040000", "quotations/inquire-investor-daily-by-market",
            {
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": iscd,
                "FID_INPUT_DATE_1": end_yyyymmdd,
                "FID_INPUT_ISCD_1": iscd1,
                "FID_INPUT_DATE_2": end_yyyymmdd,
                "FID_INPUT_ISCD_2": iscd,
            }, custtype="P")
        if not data or data.get('rt_cd') != '0':
            log.warning("[투자자동향] %s 조회 실패: %s", name,
                        (data or {}).get('msg1'))
            return {}          # 반쪽 데이터 방지 — 양 시장 모두 성공해야 저장
        for r in data.get('output') or []:
            d = r.get('stck_bsop_date') or ''
            if len(d) != 8:
                continue
            iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            row = merged.setdefault(iso, {'base_date': iso})
            try:
                for suffix, key in FIELDS:
                    row[f'{name}_{suffix}'] = int(r[key])
            except (KeyError, ValueError, TypeError):
                merged.pop(iso, None)
    # 양 시장 6개 값이 모두 있는 완전한 행만
    return {k: v for k, v in merged.items() if len(v) == 7}


def _upsert(sb, rows: list) -> int:
    if not rows:
        return 0
    sb.table('market_investor_flow').upsert(rows, on_conflict='base_date').execute()
    return len(rows)


def run(days: int = 14) -> int:
    """최근 days일 멱등 upsert (일일 잡)"""
    sb = get_supabase_client()
    merged = _fetch_window(datetime.now().strftime('%Y%m%d'))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = [v for k, v in sorted(merged.items()) if k >= cutoff]
    n = _upsert(sb, rows)
    if rows:
        log.info("[투자자동향] %s~%s %d건 upsert",
                 rows[0]['base_date'], rows[-1]['base_date'], n)
    else:
        log.warning("[투자자동향] 수집 결과 없음")
    return n


def backfill(start_yyyymmdd: str) -> int:
    """start일부터 현재까지 백필 — 300거래일 윈도를 과거로 이동하며 반복"""
    sb = get_supabase_client()
    start_iso = f"{start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:8]}"
    end = datetime.now().strftime('%Y%m%d')
    total = 0
    while True:
        merged = _fetch_window(end)
        if not merged:
            break
        rows = [v for k, v in sorted(merged.items()) if k >= start_iso]
        total += _upsert(sb, rows)
        oldest = min(merged)
        log.info("[투자자동향 백필] 윈도 %s 까지 %d건 (누계 %d)", oldest, len(rows), total)
        if oldest <= start_iso or len(rows) < len(merged):
            break
        end = (date.fromisoformat(oldest) - timedelta(days=1)).strftime('%Y%m%d')
    log.info("[투자자동향 백필] 완료: 총 %d건", total)
    return total


if __name__ == '__main__':
    if '--backfill' in sys.argv:
        idx = sys.argv.index('--backfill')
        backfill(sys.argv[idx + 1] if len(sys.argv) > idx + 1 else '20240101')
    else:
        run()
