"""
collect_market.py
─────────────────
KIS API → Supabase market_data 테이블 수집기

실행:
    python collect_market.py              # companies 테이블 기준
    python collect_market.py --all-listed # 전체 상장사 (시간 오래 걸림)

스케줄 (run_all.py에서):
    토요일 오전 9시 (장 마감 후 데이터 기준)
"""

import os
import sys
import time
import logging
from logger_config import get_logger
log = get_logger(__name__)

from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from db_client import get_supabase_client
from db_utils import fetch_all_pages as _fetch_all_pages

# 기존 managers, stock_api 활용
try:
    from managers import kis_auth, get_session, safe_float, safe_int
    import stock_api
except ImportError:
    print("managers.py, stock_api.py가 같은 폴더에 있어야 합니다.")
    sys.exit(1)


from format_utils import parse_date as _parse_date   # 중복 제거 — format_utils 통합

# SB_URL / SB_SERVICE_KEY: db_client.get_supabase_client()에서 검증
SB_URL         = os.getenv("SB_URL", "")
SB_SERVICE_KEY = os.getenv("SB_SERVICE_KEY", "")


def collect_market_one(code: str, name: str) -> Optional[dict]:
    """단일 종목 시장 데이터 수집 (KIS API)"""
    clean_code = code.split(".")[0]
    try:
        # 현재가 + 기본 정보
        raw = stock_api.get_raw_price(clean_code)
        if not raw:
            return None

        # get_raw_price는 output 없이 바로 딕셔너리 반환
        output = raw.get("output", raw)

        mkt_nm = output.get("rprs_mrkt_kor_name", "")
        if "KOSPI" in mkt_nm or "코스피" in mkt_nm:
            market = "KOSPI"
        else:
            market = "KOSDAQ"

        row = {
            "stock_code":        clean_code,
            "corp_name":         name,
            "market":            market,
            "base_date":         str(date.today()),
            # ── 현재가 / 등락 ──
            "price":             safe_int(output.get("stck_prpr"),            zero_as_none=True),
            "price_change":      safe_int(output.get("prdy_vrss")),
            "price_change_sign": output.get("prdy_vrss_sign") or None,
            "price_change_rate": safe_float(output.get("prdy_ctrt")),
            "volume_change_rate":safe_float(output.get("prdy_vrss_vol_rate"), zero_as_none=True),
            # ── OHLC + 가격 범위 ──
            "high_price":        safe_int(output.get("stck_hgpr"),            zero_as_none=True),
            "low_price":         safe_int(output.get("stck_lwpr"),            zero_as_none=True),
            "vwap":              safe_int(output.get("wghn_avrg_stck_prc"),   zero_as_none=True),
            # ── 거래량 / 시총 ──
            "volume":            safe_int(output.get("acml_vol"),             zero_as_none=True),
            "trading_value":     safe_int(output.get("acml_tr_pbmn"),         zero_as_none=True),
            "market_cap":        safe_int(output.get("hts_avls"),             zero_as_none=True),
            "listing_shares":    safe_int(output.get("lstn_stcn"),            zero_as_none=True),
            "vol_turnover":      safe_float(output.get("vol_tnrt"),           zero_as_none=True),
            # ── 밸류에이션 ──
            "per":               safe_float(output.get("per"),                zero_as_none=True),
            "pbr":               safe_float(output.get("pbr"),                zero_as_none=True),
            "eps":               safe_int(output.get("eps"),                  zero_as_none=True),
            "bps":               safe_int(output.get("bps"),                  zero_as_none=True),
            "dps":               safe_int(output.get("dps"),                  zero_as_none=True),
            "fiscal_month":      output.get("stac_month") or None,
            # ── 수급 ──
            "foreign_hold_rate": safe_float(output.get("hts_frgn_ehrt"),      zero_as_none=True),
            "foreign_hold_qty":  safe_int(output.get("frgn_hldn_qty")),
            "foreign_net_buy":   safe_int(output.get("frgn_ntby_qty")),
            "program_net_buy":   safe_int(output.get("pgtr_ntby_qty")),
            "loan_balance_rate": safe_float(output.get("whol_loan_rmnd_rate"),zero_as_none=True),
            "short_sell_qty":    safe_int(output.get("last_ssts_cntg_qty"),   zero_as_none=True),
            # ── 52주 고저 (w52 = 정확한 52주) ──
            "w52_high":          safe_int(output.get("w52_hgpr"),             zero_as_none=True),
            "w52_low":           safe_int(output.get("w52_lwpr"),             zero_as_none=True),
            "w52_high_date":     _parse_date(output.get("w52_hgpr_date")),
            "w52_low_date":      _parse_date(output.get("w52_lwpr_date")),
            # ── 종목 상태 ──
            "market_warn_code":  output.get("mrkt_warn_cls_code") or None,
            "is_caution":        (output.get("invt_caful_yn") == "Y") or None,
            "manage_issue_code": output.get("mang_issu_cls_code") or None,
            "is_short_over":     (output.get("short_over_yn") == "Y") or None,
            "is_liquidation":    (output.get("sltr_yn") == "Y") or None,
            "hgpr_cls_code":     output.get("new_hgpr_lwpr_cls_code") or None,
        }

        # 시가총액 단위 변환 (억 → 원), fallback: 현재가 × 상장주식수
        if row["market_cap"]:
            row["market_cap"] = row["market_cap"] * 100_000_000
        else:
            _price  = safe_int(output.get("stck_prpr"),  zero_as_none=True)
            _shares = safe_int(output.get("lstn_stcn"),  zero_as_none=True)
            if _price and _shares:
                row["market_cap"] = _price * _shares
        # hgpr_cls_code: KIS API가 '신고가'/'신저가' 텍스트로 반환
        # hgpr_cls: 동일 텍스트 그대로 사용 (세부구분 없음)
        hgpr_code = row.get("hgpr_cls_code")
        row["hgpr_cls"] = hgpr_code  # '신고가' or '신저가' or None
        # None이면 키 제거 → upsert 시 기존 값 보존
        if hgpr_code is None:
            row.pop("hgpr_cls_code", None)
            row.pop("hgpr_cls",      None)

        # 수익률 — DB 과거 데이터로 calculate_returns()에서 일괄 계산
        return row

    except Exception as e:
        log.error(f"[ERROR] {name} ({clean_code}): {e}")
        return None


def calculate_returns(sb, target_codes: list = None, target_date: str = None):
    """
    DB에 쌓인 market_data 가격으로 기간별 수익률 일괄 계산·업데이트
    - 1주(5거래일), 1달(21거래일), 3달(63거래일), 1년(252거래일)
    """
    import datetime
    if target_date:
        today_str = target_date
    else:
        # 오늘 날짜 데이터가 없으면 DB 최신 날짜 자동 사용
        today_str = datetime.date.today().isoformat()
        check = sb.table("market_data").select("base_date") \
            .eq("base_date", today_str).limit(1).execute()
        if not check.data:
            latest = sb.table("market_data").select("base_date") \
                .order("base_date", desc=True).limit(1).execute()
            if latest.data:
                today_str = latest.data[0]["base_date"]
                log.info(f"[수익률] 오늘 데이터 없음 — 최신 거래일 {today_str} 기준으로 계산")

    if target_codes:
        codes = target_codes
    else:
        # 모니터링 종목만 대상으로 (전체 상장사 아님)
        mon_res = sb.table("companies").select("code") \
            .eq("is_monitored", True).execute()
        codes = [r["code"].split(".")[0] for r in (mon_res.data or [])]

    if not codes:
        log.info("[수익률] 오늘 수집 종목 없음 — 스킵")
        return

    log.info(f"[수익률] {len(codes)}개 종목 기간별 수익률 계산 시작")
    updated = 0
    PERIODS = {"week": 5, "month": 21, "quarter": 63, "year": 252}

    chunk = 10  # 종목 수 줄여서 1000건 limit 회피
    for i in range(0, len(codes), chunk):
        batch = codes[i:i+chunk]
        all_rows = _fetch_all_pages(
            sb.table("market_data")
              .select("stock_code,base_date,price")
              .in_("stock_code", batch)
              .not_.is_("price", "null")
              .order("base_date", desc=False)
        )

        hist = {}
        for r in all_rows:
            code = r["stock_code"]
            if code not in hist:
                hist[code] = []
            hist[code].append((r["base_date"], r["price"]))

        updates = []
        for code, prices in hist.items():
            if len(prices) < 2:
                continue
            current_price = prices[-1][1]
            if not current_price:
                continue
            row_update = {"stock_code": code, "base_date": today_str}
            for col, days in PERIODS.items():
                if len(prices) >= days + 1:
                    past_price = prices[-(days + 1)][1]
                    if past_price:
                        ret = round((current_price - past_price) / past_price * 100, 2)
                        row_update[f"{col}_return"] = ret
            updates.append(row_update)

        for j in range(0, len(updates), 50):
            for upd in updates[j:j+50]:
                try:
                    code = upd.pop('stock_code')
                    date = upd.pop('base_date')
                    sb.table("market_data").update(upd) \
                        .eq("stock_code", code).eq("base_date", date).execute()
                    updated += 1
                except Exception as e:
                    log.error(f"[수익률] update 오류: {e}")

    log.info(f"[수익률] 완료: {updated}개 종목 업데이트")


def run(all_listed: bool = False, max_workers: int = 5):
    """메인 수집 함수"""
    sb = get_supabase_client()  # 환경변수 미설정 시 RuntimeError 발생 (db_client에서 처리)

    log.info("=== 시장 데이터 수집 시작 ===")

    def load_companies(filter_col, filter_val):
        return _fetch_all_pages(
            sb.table("companies").select("name, code").eq(filter_col, filter_val)
        )

    if all_listed:
        log.info("전체 상장사 모드 — companies 테이블 전체 (active=True)")
        raw = load_companies("active", True)
    else:
        log.info("모니터링 종목 모드 — full+news (is_monitored=True)")
        raw = load_companies("is_monitored", True)

    seen = set()
    targets = []
    for item in raw:
        code = (item.get("code") or "").split(".")[0]
        if code and code not in seen:
            seen.add(code)
            targets.append({"code": code, "name": item["name"]})

    log.info(f"수집 대상: {len(targets)}개 종목")

    collected = []
    failed    = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(collect_market_one, t["code"], t["name"]): t
            for t in targets
        }

        for i, future in enumerate(as_completed(futures), 1):
            t = futures[future]
            try:
                row = future.result()
                if row:
                    collected.append(row)
                    log.info(f"[{i}/{len(targets)}] ✅ {t['name']} "
                             f"현재가:{row.get('price'):,}원" if row.get('price') else "")
                else:
                    failed.append(t["name"])
            except Exception as e:
                failed.append(t["name"])
                log.error(f"[{i}/{len(targets)}] ❌ {t['name']}: {e}")

            time.sleep(0.2)

    # Supabase upsert
    upserted = 0
    batch_size = 50
    for i in range(0, len(collected), batch_size):
        batch = collected[i:i+batch_size]
        try:
            sb.table("market_data").upsert(
                batch,
                on_conflict="stock_code,base_date"
            ).execute()
            upserted += len(batch)
        except Exception as e:
            log.error(f"DB 저장 실패: {e}")

    log.info(f"=== 완료: 저장 {upserted}개, 실패 {len(failed)}개 ===")
    return upserted, len(failed)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KIS 시장 데이터 수집")
    parser.add_argument("--all-listed", action="store_true")
    parser.add_argument("--workers",    type=int, default=5)
    parser.add_argument("--backfill",   type=int, default=0, help="과거 N일치 백필")
    parser.add_argument("--from",       dest="from_date", default=None, help="백필 시작일 YYYY-MM-DD")
    parser.add_argument("--to",         dest="to_date",   default=None, help="백필 종료일 YYYY-MM-DD")
    args = parser.parse_args()
    if args.from_date and args.to_date:
        backfill_market(from_date=args.from_date, to_date=args.to_date)
    elif args.backfill:
        backfill_market(days=args.backfill)
    else:
        run(all_listed=args.all_listed, max_workers=args.workers)


# ══════════════════════════════════════════════════════════
# 신고가 종목 수집 (KIS ka10017 — 주식신고가조회)
# ══════════════════════════════════════════════════════════

def fetch_new_high_stocks(market_code: str = '0000') -> list[dict]:
    """
    KIS near-new-highlow API로 52주 신고가 종목 조회
    (FHPST01870000 / ranking/near-new-highlow)
    - hprc_near_rate == 0.00 → 오늘 52주 신고가 갱신
    - hprc_near_rate < 0    → 신고가 근접 (미갱신)
    market_code: 0000=전체, 0001=거래소, 1001=코스닥
    """
    from managers import kis_auth, KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
    import requests as req

    token = kis_auth.get_token()
    if not token:
        logging.warning("[신고가] 토큰 없음")
        return []

    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/ranking/near-new-highlow"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey":    KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id":     "FHPST01870000",
        "custtype":  "P",
    }
    params = {
        "fid_cond_mrkt_div_code":  "J",
        "fid_cond_scr_div_code":   "20187",
        "fid_div_cls_code":        "0",
        "fid_input_cnt_1":         "",
        "fid_input_cnt_2":         "",
        "fid_prc_cls_code":        "0",   # 0=신고근접
        "fid_input_iscd":          market_code,
        "fid_trgt_cls_code":       "0",
        "fid_trgt_exls_cls_code":  "0",
        "fid_aply_rang_prc_1":     "",
        "fid_aply_rang_prc_2":     "",
        "fid_aply_rang_vol":       "0",
    }

    try:
        res = req.get(url, headers=headers, params=params, timeout=10)
        data = res.json()
        if data.get('rt_cd') != '0':
            logging.warning(f"[신고가] API 오류 (mkt={market_code}): rt_cd={data.get('rt_cd')} msg={data.get('msg1')}")
            return []
        logging.info(f"[신고가] API 응답 (mkt={market_code}): output {len(data.get('output') or [])}개")

        result = []
        for row in (data.get('output') or []):
            code  = row.get('mksc_shrn_iscd', '').strip()
            name  = row.get('hts_kor_isnm', '').strip()
            price = int(float(row.get('stck_prpr', 0) or 0))
            chg   = float(row.get('prdy_ctrt', 0) or 0)
            near_raw = row.get('hprc_near_rate', '')
            try:
                near = float(near_raw) if near_raw not in ('', None) else None
            except (ValueError, TypeError):
                near = None
            high  = int(float(row.get('new_hgpr', 0) or 0))
            low   = int(float(row.get('new_lwpr', 0) or 0))

            if not code:
                continue

            # 52주 신고가 갱신 종목만 수집 (near_rate == 0.00 → 오늘 갱신)
            # near_raw가 빈 문자열인 경우도 0으로 처리 (신고가 갱신 당일 API 응답)
            if near is not None and near != 0.0:
                continue   # 근접(미갱신) 포함하지 않음
            cls_label = '신고가'
            cls_code  = '1'

            result.append({
                'code':          code,
                'name':          name,
                'price':         price,
                'chg_pct':       chg,
                'new_hgpr_cls':  cls_label,
                'new_hgpr_code': cls_code,
                'd52_high':      high,
                'd52_low':       low,
            })

        logging.info(f"[신고가] {market_code} → {len(result)}개 (갱신+근접)")
        return result

    except Exception as e:
        logging.error(f"[신고가] 조회 오류: {e}")
        return []


def save_new_high_to_db(rows: list[dict], base_date: str, sb_client=None):
    """신고가 정보를 market_data.hgpr_cls_code 컬럼에 업데이트

    오전 run_market()이 FHKST01010100 기반으로 hgpr_cls_code를 설정했다가
    장중에 신고가에서 벗어나도 pop() 로직으로 기존값이 보존되는 문제 방지를 위해
    ① 오늘 전체 hgpr_cls_code를 null로 초기화
    ② near-new-highlow API 결과 종목만 재설정
    순서로 처리한다.
    """
    if not rows:
        logging.warning("[신고가] 저장할 데이터 없음")
        return
    # sb_client 없으면 직접 생성
    if sb_client is None:
        sb_client = get_supabase_client()

    # ① 오늘 전체 초기화 — 오전 수집에서 잘못 설정된 stale 값 제거
    try:
        sb_client.table('market_data') \
            .update({'hgpr_cls_code': None, 'hgpr_cls': None}) \
            .eq('base_date', base_date) \
            .not_.is_('hgpr_cls_code', 'null') \
            .execute()
        logging.info(f"[신고가] {base_date} hgpr_cls_code 초기화 완료")
    except Exception as e:
        logging.warning(f"[신고가] 초기화 실패 (이어서 진행): {e}")

    # ② near-new-highlow API 결과만 재설정
    updated = 0
    for r in rows:
        code = r['code'].strip()
        cls_code  = r.get('new_hgpr_code', '')
        cls_label = {'1':'신고가', '0':'신고가 근접'}.get(cls_code, cls_code)
        upd = {'hgpr_cls_code': cls_label, 'hgpr_cls': cls_label}
        try:
            res = sb_client.table('market_data').update(upd) \
                .eq('stock_code', code).eq('base_date', base_date).execute()
            if res.data:
                updated += len(res.data)
        except Exception as e:
            logging.debug(f"[신고가] {code} 업데이트 실패: {e}")
    logging.info(f"[신고가] {updated}/{len(rows)}개 market_data 업데이트")


def collect_new_high():
    """KIS API로 코스피+코스닥 52주 신고가 수집 후 저장"""
    today = date.today().isoformat()
    all_rows = []
    for mkt in ['0001', '1001']:   # 코스피, 코스닥
        all_rows.extend(fetch_new_high_stocks(mkt))
        time.sleep(0.3)
    # 중복 제거 (code 기준)
    seen = set()
    deduped = []
    for r in all_rows:
        if r['code'] not in seen:
            seen.add(r['code'])
            deduped.append(r)
    logging.info(f"[신고가] 전체 {len(deduped)}개 (코스피+코스닥)")
    sb_client = get_supabase_client()
    save_new_high_to_db(deduped, today, sb_client)
    return deduped


# ══════════════════════════════════════════════════════════
# 기관/외국인 매매종목 가집계 (FHPTJ04400000)
# ══════════════════════════════════════════════════════════

def fetch_foreign_institution(
    market_code: str = '0000',   # 0000:전체, 0001:코스피, 1001:코스닥
    sort_cls:    str = '0',      # 0:순매수상위, 1:순매도상위
    etc_cls:     str = '0',      # 0:전체, 1:외국인, 2:기관계, 3:기타
    div_cls:     str = '1',      # 0:수량정렬, 1:금액정렬
) -> list[dict]:
    """기관/외국인 매매종목 가집계 조회
    장중 집계 시간: 외국인 09:30/11:20/13:20/14:30 / 기관 10:00/11:20/13:20/14:30
    """
    from managers import kis_auth, KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
    import requests as req

    token = kis_auth.get_token()
    if not token:
        logging.warning("[수급] 토큰 없음")
        return []

    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/foreign-institution-total"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey":    KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id":     "FHPTJ04400000",
        "custtype":  "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE":  "V",
        "FID_COND_SCR_DIV_CODE":   "16449",
        "FID_INPUT_ISCD":          market_code,
        "FID_DIV_CLS_CODE":        div_cls,
        "FID_RANK_SORT_CLS_CODE":  sort_cls,
        "FID_ETC_CLS_CODE":        etc_cls,
    }

    try:
        res = req.get(url, headers=headers, params=params, timeout=10)
        data = res.json()
        if data.get('rt_cd') != '0':
            logging.warning(f"[수급] API 오류: {data.get('msg1')}")
            return []

        result = []
        for row in (data.get('output') or []):
            code = row.get('mksc_shrn_iscd', '').strip()
            if not code:
                continue
            result.append({
                'code':         code,
                'name':         row.get('hts_kor_isnm', '').strip(),
                'price':        int(float(row.get('stck_prpr', 0) or 0)),
                'chg_pct':      float(row.get('prdy_ctrt', 0) or 0),
                'volume':       int(float(row.get('acml_vol', 0) or 0)),
                # 수량 기준
                'frgn_net_buy': int(float(row.get('frgn_ntby_qty', 0) or 0)),
                'orgn_net_buy': int(float(row.get('orgn_ntby_qty', 0) or 0)),
                'ivtr_net_buy': int(float(row.get('ivtr_ntby_qty', 0) or 0)),  # 투자신탁
                'fund_net_buy': int(float(row.get('fund_ntby_qty', 0) or 0)),  # 기금
                # 금액 기준 (백만원)
                'frgn_net_buy_amt': int(float(row.get('frgn_ntby_tr_pbmn', 0) or 0)),
                'orgn_net_buy_amt': int(float(row.get('orgn_ntby_tr_pbmn', 0) or 0)),
            })

        logging.info(f"[수급] {market_code}/etc={etc_cls} → {len(result)}개")
        return result

    except Exception as e:
        logging.error(f"[수급] 조회 오류: {e}")
        return []


def collect_foreign_institution() -> dict:
    """외국인/기관 동시 순매수 상위 수집 (오늘의 시황용)
    반환: { 'frgn_buy': [...], 'orgn_buy': [...], 'both_buy': [...] }
    """
    # 외국인 순매수 상위 (금액 기준)
    frgn = fetch_foreign_institution(market_code='0000', sort_cls='0', etc_cls='1')
    time.sleep(0.3)
    # 기관 순매수 상위
    orgn = fetch_foreign_institution(market_code='0000', sort_cls='0', etc_cls='2')

    # 외국인/기관 동시 순매수 종목
    frgn_codes = {r['code'] for r in frgn if r['frgn_net_buy_amt'] > 0}
    both = [r for r in orgn if r['code'] in frgn_codes and r['orgn_net_buy_amt'] > 0]

    # Supabase에 저장 (market_data에 기관 순매수 업데이트)
    today = date.today().isoformat()
    if SB_URL and SB_SERVICE_KEY:
        sb_client = get_supabase_client()
        updated = 0
        # frgn 코드 집합 (외국인 기준 API로 조회된 종목 — 수급 신뢰 가능)
        frgn_codes_set = {r['code'] for r in frgn}

        # 외국인 + 기관 병합
        all_rows = {r['code']: r for r in frgn}
        for r in orgn:
            if r['code'] in all_rows:
                all_rows[r['code']]['orgn_net_buy'] = r['orgn_net_buy']
                all_rows[r['code']]['orgn_net_buy_amt'] = r['orgn_net_buy_amt']
            else:
                all_rows[r['code']] = r

        for code, r in all_rows.items():
            try:
                upd = {}
                # foreign_net_buy: 외국인 기준 API(frgn)에 포함된 종목만 갱신.
                # 기관 기준 API(orgn)에서 반환된 frgn_ntby_qty는 부정확(0)할 수 있어
                # 기존 DB 값을 덮어쓰지 않음.
                if code in frgn_codes_set and r.get('frgn_net_buy') is not None:
                    upd['foreign_net_buy'] = r['frgn_net_buy']
                if r.get('orgn_net_buy') is not None:
                    upd['institution_net_buy'] = r['orgn_net_buy']
                if upd:
                    res = sb_client.table('market_data').update(upd)                         .eq('stock_code', code).eq('base_date', today).execute()
                    if res.data:
                        updated += 1
            except Exception as e:
                logging.warning(f"[수급] {code} 업데이트 실패: {e}")
        logging.info(f"[수급] {updated}개 market_data 수급 업데이트")

    return {
        'frgn_buy': frgn[:20],
        'orgn_buy': orgn[:20],
        'both_buy': both[:10],
        'collected_at': date.today().isoformat(),
    }


# ══════════════════════════════════════════════════════════
# 90일 백필 (FHKST03010100 일별시세 + FHKST01010100 보완)
# ══════════════════════════════════════════════════════════

def backfill_market(days: int = 90, max_workers: int = 3,
                    from_date: str = None, to_date: str = None):
    """
    과거 N일치 market_data 백필
    Step1: FHKST03010100으로 종목별 일별 OHLCV + 기본 밸류 수집
    Step2: FHKST01010100(오늘)의 수급/52주/종목상태 등 오늘값을 전 날짜에 공통 적용

    from_date/to_date: 'YYYY-MM-DD' 형식으로 날짜 범위 지정 가능
    """
    from managers import kis_auth, KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
    import requests as req
    import datetime

    if not SB_URL or not SB_SERVICE_KEY:
        log.error("[백필] 환경변수 없음")
        return

    sb_client = get_supabase_client()
    token = kis_auth.get_token()
    if not token:
        log.error("[백필] 토큰 없음")
        return

    # 수집 대상: 전체 상장사 (active=True)
    all_res = []
    page = 0
    while True:
        res = sb_client.table('companies').select('code,name')             .eq('active', True)             .range(page * 1000, (page + 1) * 1000 - 1).execute()
        if not res.data:
            break
        all_res.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    targets = [{'code': r['code'].split('.')[0], 'name': r['name']}
               for r in all_res if r.get('code')]

    if from_date and to_date:
        log.info(f"[백필] 대상: {len(targets)}개 종목, {from_date} ~ {to_date}")
        date_from = from_date.replace('-', '')
        date_to   = to_date.replace('-', '')
    else:
        log.info(f"[백필] 대상: {len(targets)}개 종목 (전체 상장사), {days}일치")
        end_dt   = datetime.date.today()
        start_dt = end_dt - datetime.timedelta(days=days + 30)  # 여유있게
        date_from = start_dt.strftime('%Y%m%d')
        date_to   = end_dt.strftime('%Y%m%d')

    headers = {
        'authorization': f'Bearer {token}',
        'appkey':    KIS_APP_KEY,
        'appsecret': KIS_APP_SECRET,
        'tr_id':     'FHKST03010100',
        'custtype':  'P',
    }

    # Step1: 종목별 일별 시세 수집
    def fetch_daily(t):
        code, name = t['code'], t['name']
        params = {
            'FID_COND_MRKT_DIV_CODE': 'J',
            'FID_INPUT_ISCD':         code,
            'FID_INPUT_DATE_1':       date_from,
            'FID_INPUT_DATE_2':       date_to,
            'FID_PERIOD_DIV_CODE':    'D',
            'FID_ORG_ADJ_PRC':        '0',  # 수정주가
        }
        try:
            res = req.get(
                f'{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice',
                headers=headers, params=params, timeout=10
            )
            data = res.json()
            if data.get('rt_cd') != '0':
                return code, name, []

            output2 = data.get('output2', []) or []
            output1 = data.get('output1', {}) or {}

            rows = []
            for r in output2:
                d = r.get('stck_bsop_date', '')
                if not d or len(d) != 8:
                    continue
                base_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                price = safe_int(r.get('stck_clpr'), zero_as_none=True)
                if not price:
                    continue

                rows.append({
                    'stock_code':    code,
                    'corp_name':     name,
                    'base_date':     base_date,
                    # 시세
                    'price':         price,
                    'high_price':    safe_int(r.get('stck_hgpr'),   zero_as_none=True),
                    'low_price':     safe_int(r.get('stck_lwpr'),   zero_as_none=True),
                    'volume':        safe_int(r.get('acml_vol'),    zero_as_none=True),
                    'trading_value': safe_int(r.get('acml_tr_pbmn'),zero_as_none=True),
                    'vol_turnover':  safe_float(r.get('vol_tnrt'),  zero_as_none=True),
                    # 밸류 (output2에도 포함)
                    'market_cap':    safe_int(r.get('hts_avls'),    zero_as_none=True),
                    'per':           safe_float(r.get('per'),        zero_as_none=True),
                    'eps':           safe_float(r.get('eps'),        zero_as_none=True),
                    'pbr':           safe_float(r.get('pbr'),        zero_as_none=True),
                    'loan_balance_rate': safe_float(r.get('itewhol_loan_rmnd_ratem'), zero_as_none=True),
                    'listing_shares':safe_int(r.get('lstn_stcn'),   zero_as_none=True),
                })

            # 시총 단위 변환
            for row in rows:
                if row.get('market_cap'):
                    row['market_cap'] = row['market_cap'] * 100_000_000

            return code, name, rows
        except Exception as e:
            log.error(f"[백필] {name}({code}) 오류: {e}")
            return code, name, []

    all_rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_daily, t): t for t in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            code, name, rows = fut.result()
            log.info(f"[백필] [{i}/{len(targets)}] {name}: {len(rows)}일치")
            all_rows.extend(rows)
            time.sleep(0.3)

    if not all_rows:
        log.warning("[백필] 수집 데이터 없음")
        return

    # Step2: 오늘 FHKST01010100 값으로 수급/52주/종목상태 보완
    # → 모니터링 종목만 (전체 상장사 수천 종목 API 호출 방지)
    log.info("[백필] Step2: 모니터링 종목만 수급/52주/종목상태 보완")
    mon_res2 = sb_client.table('companies')         .select('code,name').eq('is_monitored', True).execute()
    mon_targets = [{'code': r['code'].split('.')[0], 'name': r['name']}
                   for r in (mon_res2.data or []) if r.get('code')]
    supplement = {}  # code → 오늘 row
    for t in mon_targets:
        today_row = collect_market_one(t['code'], t['name'])
        if today_row:
            supplement[t['code']] = today_row
        time.sleep(0.2)

    SUPPLEMENT_FIELDS = [
        'market', 'price_change_sign',
        'foreign_hold_rate', 'foreign_hold_qty', 'foreign_net_buy',
        'program_net_buy', 'short_sell_qty',
        'w52_high', 'w52_low', 'w52_high_date', 'w52_low_date',
        'market_warn_code', 'is_caution',
        'manage_issue_code', 'is_short_over', 'is_liquidation',
        'hgpr_cls_code', 'hgpr_cls', 'fiscal_month',
        'vwap',
        'bps', 'dps',
    ]
    for row in all_rows:
        sup = supplement.get(row['stock_code'], {})
        for field in SUPPLEMENT_FIELDS:
            if field in sup and row.get(field) is None:
                row[field] = sup[field]

    # Step3: price_change_rate 계산 (백필 API는 전일대비율 미제공)
    # 종목별로 날짜 정렬 후 연속 price 값으로 계산
    log.info("[백필] Step3: price_change_rate 계산")
    from collections import defaultdict
    stock_rows = defaultdict(list)
    for row in all_rows:
        stock_rows[row['stock_code']].append(row)

    calc_count = 0
    for code, rows in stock_rows.items():
        rows.sort(key=lambda r: r['base_date'])
        for i, row in enumerate(rows):
            if row.get('price_change_rate') is not None:
                continue  # 이미 값 있으면 스킵
            if i == 0:
                continue  # 첫 행은 전일 없음
            prev_price = rows[i - 1].get('price')
            cur_price  = row.get('price')
            if prev_price and cur_price and prev_price > 0:
                row['price_change_rate'] = round((cur_price - prev_price) / prev_price * 100, 2)
                calc_count += 1
    log.info(f"[백필] price_change_rate 계산 완료: {calc_count}건")

    # DB upsert
    log.info(f"[백필] DB upsert 시작: {len(all_rows)}건")
    upserted = 0
    for i in range(0, len(all_rows), 100):
        batch = all_rows[i:i+100]
        try:
            sb_client.table('market_data').upsert(
                batch, on_conflict='stock_code,base_date'
            ).execute()
            upserted += len(batch)
        except Exception as e:
            log.error(f"[백필] upsert 오류: {e}")

    log.info(f"[백필] 완료: {upserted}/{len(all_rows)}건 저장")
    return upserted


if __name__ == '__main__':
    # 직접 실행:
    #   python3 collect_market.py --backfill 90
    #   python3 collect_market.py --from 2026-06-09 --to 2026-06-12
    import argparse, sys
    parser = argparse.ArgumentParser()
    parser.add_argument('--backfill', type=int, default=0)
    parser.add_argument('--from', dest='from_date', default=None)
    parser.add_argument('--to',   dest='to_date',   default=None)
    args, _ = parser.parse_known_args()
    if args.from_date and args.to_date:
        backfill_market(from_date=args.from_date, to_date=args.to_date)
    elif args.backfill:
        backfill_market(days=args.backfill)


# ══════════════════════════════════════════════════════════
# 증권사 투자의견 수집 (FHKST663300C0)
# ══════════════════════════════════════════════════════════

def fetch_analyst_opinions(stock_code: str, corp_name: str,
                           from_date: str = None, to_date: str = None) -> list[dict]:
    """
    종목별 증권사 투자의견 조회
    from_date/to_date: 'YYYYMMDD' 형식, 기본값 오늘
    """
    from managers import kis_auth, KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
    import requests as req

    token = kis_auth.get_token()
    if not token:
        return []

    today = date.today().strftime('%Y%m%d')
    from_date = from_date or today
    to_date   = to_date   or today

    headers = {
        'authorization': f'Bearer {token}',
        'appkey':    KIS_APP_KEY,
        'appsecret': KIS_APP_SECRET,
        'tr_id':     'FHKST663300C0',
        'custtype':  'P',
    }
    params = {
        'FID_COND_MRKT_DIV_CODE': 'J',
        'FID_COND_SCR_DIV_CODE':  '16633',
        'FID_INPUT_ISCD':         stock_code,
        'FID_INPUT_DATE_1':       from_date,
        'FID_INPUT_DATE_2':       to_date,
    }

    try:
        res = req.get(
            f'{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/invest-opinion',
            headers=headers, params=params, timeout=10
        )
        data = res.json()
        if data.get('rt_cd') != '0':
            logging.warning(f"[투자의견] {corp_name}({stock_code}) API 오류: {data.get('msg1')}")
            return []

        result = []
        for row in (data.get('output') or []):
            d = row.get('stck_bsop_date', '')
            if not d:
                continue
            opinion_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            tp = row.get('hts_goal_prc', '0').strip()
            bp = row.get('stck_prdy_clpr', '0').strip()
            gr = row.get('dprt', '').strip()
            result.append({
                'stock_code':        stock_code,
                'corp_name':         corp_name,
                'opinion_date':      opinion_date,
                'firm_name':         row.get('mbcr_name', '').strip(),
                'opinion':           row.get('invt_opnn', '').strip(),
                'opinion_code':      row.get('invt_opnn_cls_code', '').strip(),
                'prev_opinion':      row.get('rgbf_invt_opnn', '').strip(),
                'prev_opinion_code': row.get('rgbf_invt_opnn_cls_code', '').strip(),
                'target_price':      int(tp) if tp.lstrip('-').isdigit() else None,
                'gap_rate':          float(gr) if gr else None,
            })
        return result

    except Exception as e:
        logging.error(f"[투자의견] {corp_name}({stock_code}) 오류: {e}")
        return []


def collect_analyst_opinions(from_date: str = None, to_date: str = None) -> int:
    """
    모니터링 종목 전체 투자의견 수집 후 DB 저장
    기본값: 오늘 하루치
    """
    if not SB_URL or not SB_SERVICE_KEY:
        logging.error("[투자의견] 환경변수 없음")
        return 0

    sb_client = get_supabase_client()
    today = date.today().strftime('%Y%m%d')
    from_date = from_date or today
    to_date   = to_date   or today

    # 모니터링 종목 조회
    res = sb_client.table('companies') \
        .select('code,name').eq('is_monitored', True).execute()
    targets = [{'code': r['code'].split('.')[0], 'name': r['name']}
               for r in (res.data or []) if r.get('code')]

    logging.info(f"[투자의견] {len(targets)}개 종목 수집 시작 ({from_date}~{to_date})")

    all_rows = []
    for t in targets:
        rows = fetch_analyst_opinions(t['code'], t['name'], from_date, to_date)
        all_rows.extend(rows)
        time.sleep(0.2)

    if not all_rows:
        logging.info("[투자의견] 수집 데이터 없음")
        return 0

    # upsert (중복 방지)
    saved = 0
    for i in range(0, len(all_rows), 50):
        batch = all_rows[i:i+50]
        try:
            sb_client.table('analyst_opinions').upsert(
                batch, on_conflict='stock_code,opinion_date,firm_name'
            ).execute()
            saved += len(batch)
        except Exception as e:
            logging.error(f"[투자의견] upsert 오류: {e}")

    logging.info(f"[투자의견] {saved}/{len(all_rows)}건 저장 완료")

    # 새 의견 감지 (오늘 날짜 데이터 중 신규 목표가 변경 건)
    if from_date == today:
        changed = [r for r in all_rows
                   if r.get('target_price') and r.get('target_price') != r.get('base_price')
                   and r.get('opinion_code') in ('1','2')]
        if changed:
            logging.info(f"[투자의견] 오늘 매수의견 {len(changed)}건 감지")
            for r in changed[:5]:
                logging.info(f"  {r['corp_name']} | {r['firm_name']} | {r['opinion']} | 목표가:{r['target_price']:,}원 | 괴리율:{r['gap_rate']}%")

    return saved
