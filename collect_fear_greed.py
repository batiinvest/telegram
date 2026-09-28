"""
collect_fear_greed.py
─────────────────────
한국 피어앤그리드(Fear & Greed) 원재료 → fear_greed_daily 저장 (계산은 프론트 fear-greed.js)

[원본] 태린이아빠 「한국 피어앤그리드오실레이터」(2026-09-18 자료) — 5요소 × 20%
  ① 125일 모멘텀(지수)  ② ATM 풋/콜(5일 평균 매수 거래량)  ③ VKOSPI
  ④ 10년 국채선물지수 − 5년 국채선물 추종 지수  ⑤ RSI 10일
  원본 엑셀(DataGuide)과 같은 값을 KIS에서 받는다 (2026-09-28 실측, 245거래일 대조):

  kospi / kosdaq / vkospi / bond10 / bond3 — KIS 업종 일자별지수(FHKUP03500100)
      iscd 0001 / 1001 / 0503(VKOSPI) / 6010(10년국채선물지수) / 6008(국채선물지수=3년)
      → 엑셀과 오차 0.
  bond5 — '5년 국채선물 추종 지수'(FnGuide IKG245)는 KIS·KRX(로그인 필요)에서 못 받는다.
      일간 수익률이 3년 국채선물지수 70% + 10년 30% 합성과 R²=0.999999로 같아
      전일 값에 (0.7·r3 + 0.3·r10)을 곱해 이어 붙인다(1년 누적 오차 최대 0.03pt / ≈970pt).
      기준점 = 원본 엑셀 2026-09-28 종가 967.10.
  call_vol / put_vol — 코스피200 옵션 콜·풋 매수 거래량 합계(계약).
      KIS 시장별 투자자매매동향(시세, FHPTJ04030000) K2I + OC01/OP01의
      외국인·개인·기관·기타법인 매수량 합 = 원본 엑셀 '전체' 열과 일치.
      당일 값만 주는 API라 과거분은 원본 엑셀로 1회 시드(seed_options), 이후 매일 쌓는다.

실행:
  python3 collect_fear_greed.py                       # 최근 지수 + 당일 옵션 (일일 잡)
  python3 collect_fear_greed.py --backfill 20250401   # 지수·bond5 백필
  python3 collect_fear_greed.py --seed-options f.json # {"YYYY-MM-DD": [call, put]} 시드
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from managers import kis_auth

log = get_logger(__name__)

TABLE = 'fear_greed_daily'
INDEXES = (('kospi', '0001'), ('kosdaq', '1001'), ('vkospi', '0503'),
           ('bond10', '6010'), ('bond3', '6008'))
BOND5_W3, BOND5_W10 = 0.7, 0.3
BOND5_ANCHOR = ('2026-09-28', 967.10)   # 원본 엑셀(DataGuide IKG245) 종가
# 옵션 매수 거래량 합계에 넣는 투자자(원본 엑셀 '전체' = 기관 합계 + 기타법인 + 개인 + 외국인 합계)
OPT_BUYERS = ('frgn_shnu_vol', 'prsn_shnu_vol', 'orgn_shnu_vol', 'etc_corp_shnu_vol')


def _kst_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def _fetch_index(iscd: str, start: str, end: str) -> dict:
    """지수 일별 종가 {iso: float}. 1콜 ≈50거래일 → end를 과거로 옮기며 start까지."""
    out = {}
    for _ in range(40):
        body = kis_auth.kis_get(
            "FHKUP03500100", "quotations/inquire-index-daily-price",
            {"fid_cond_mrkt_div_code": "U", "fid_input_iscd": iscd,
             "fid_input_date_1": start, "fid_input_date_2": end,
             "fid_period_div_code": "D"}, custtype="P")
        if not body or body.get('rt_cd') != '0':
            log.warning("[피어앤그리드] 지수 %s 조회 실패: %s", iscd, (body or {}).get('msg1'))
            return {}
        rows = [r for r in body.get('output2') or [] if len(r.get('stck_bsop_date') or '') == 8]
        for r in rows:
            d = r['stck_bsop_date']
            try:
                v = float(r['bstp_nmix_prpr'])
            except (KeyError, ValueError, TypeError):
                continue
            if v > 0 and start <= d <= end:
                out[f"{d[:4]}-{d[4:6]}-{d[6:8]}"] = v
        if not rows:
            break
        oldest = min(r['stck_bsop_date'] for r in rows)
        if oldest <= start:
            break
        end = (datetime.strptime(oldest, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
    return out


def _fetch_indexes(start: str, end: str) -> dict:
    """5개 지수 병합 {iso: {kospi,...,bond3}} — 다섯 값이 다 있는 날만 (반쪽 행 방지)"""
    merged = {}
    for col, iscd in INDEXES:
        series = _fetch_index(iscd, start, end)
        if not series:
            return {}
        for d, v in series.items():
            merged.setdefault(d, {})[col] = v
    return {d: r for d, r in merged.items() if len(r) == len(INDEXES)}


def _chain_bond5(rows: dict, known: dict) -> None:
    """rows {iso: {bond3,bond10,...}}에 bond5를 채운다.
    known {iso: bond5}(DB 기존값·기준점)에서 앞뒤로 이어 붙인다 — 기존값은 덮지 않는다."""
    dates = sorted(rows)
    for d in dates:
        if d in known:
            rows[d]['bond5'] = known[d]
    # 앞으로: 전일 bond5 × (1 + 0.7·r3 + 0.3·r10)
    for i in range(1, len(dates)):
        cur, prv = rows[dates[i]], rows[dates[i - 1]]
        if 'bond5' not in cur and 'bond5' in prv:
            r3 = cur['bond3'] / prv['bond3'] - 1
            r10 = cur['bond10'] / prv['bond10'] - 1
            cur['bond5'] = round(prv['bond5'] * (1 + BOND5_W3 * r3 + BOND5_W10 * r10), 4)
    # 뒤로: 기준점보다 앞선 구간(백필)
    for i in range(len(dates) - 2, -1, -1):
        cur, nxt = rows[dates[i]], rows[dates[i + 1]]
        if 'bond5' not in cur and 'bond5' in nxt:
            r3 = nxt['bond3'] / cur['bond3'] - 1
            r10 = nxt['bond10'] / cur['bond10'] - 1
            cur['bond5'] = round(nxt['bond5'] / (1 + BOND5_W3 * r3 + BOND5_W10 * r10), 4)


def _known_bond5(sb, start_iso: str) -> dict:
    res = (sb.table(TABLE).select('base_date,bond5')
           .gte('base_date', start_iso).not_.is_('bond5', 'null')
           .order('base_date').limit(1000).execute())
    known = {r['base_date']: float(r['bond5']) for r in res.data or []}
    known.setdefault(*BOND5_ANCHOR)
    return known


def _upsert_indexes(sb, rows: dict) -> int:
    cols = [c for c, _ in INDEXES] + ['bond5']
    payload = [{'base_date': d, **{c: r[c] for c in cols}}
               for d, r in sorted(rows.items()) if 'bond5' in r]
    skipped = len(rows) - len(payload)
    if skipped:
        log.warning("[피어앤그리드] bond5를 잇지 못한 날 %d일 제외 (앞뒤 기준값 없음)", skipped)
    for i in range(0, len(payload), 500):
        sb.table(TABLE).upsert(payload[i:i + 500], on_conflict='base_date').execute()
    return len(payload)


def _fetch_options() -> tuple:
    """당일 코스피200 옵션 (콜, 풋) 매수 거래량 합계. 실패 시 (None, None)."""
    vols = []
    for cls in ('OC01', 'OP01'):
        body = kis_auth.kis_get(
            "FHPTJ04030000", "quotations/inquire-investor-time-by-market",
            {"FID_INPUT_ISCD": "K2I", "FID_INPUT_ISCD_2": cls}, custtype="P")
        out = (body or {}).get('output') or []
        if not body or body.get('rt_cd') != '0' or not out:
            log.warning("[피어앤그리드] 옵션 %s 조회 실패: %s", cls, (body or {}).get('msg1'))
            return None, None
        try:
            vols.append(sum(int(out[0][k]) for k in OPT_BUYERS))
        except (KeyError, ValueError, TypeError) as e:
            log.warning("[피어앤그리드] 옵션 %s 필드 이상: %s", cls, e)
            return None, None
    return vols[0], vols[1]


def run(days: int = 20) -> int:
    """최근 days일 지수 멱등 upsert + 오늘이 거래일이면 당일 옵션 거래량 (일일 잡)"""
    sb = get_supabase_client()
    today = _kst_today()
    start = today - timedelta(days=days)
    rows = _fetch_indexes(start.strftime('%Y%m%d'), today.strftime('%Y%m%d'))
    if not rows:
        log.warning("[피어앤그리드] 지수 수집 결과 없음")
        return 0
    # bond5를 이을 기준: 창 앞쪽 날의 DB 값 (창보다 넉넉히 과거부터)
    _chain_bond5(rows, _known_bond5(sb, (start - timedelta(days=10)).isoformat()))
    n = _upsert_indexes(sb, rows)
    last = max(rows)
    log.info("[피어앤그리드] 지수 %d일 upsert (최신 %s)", n, last)

    # 옵션 거래량은 '지금' 값만 주므로, 최신 거래일이 오늘일 때만 오늘 날짜로 저장
    if last == today.isoformat():
        call_vol, put_vol = _fetch_options()
        if call_vol and put_vol:
            sb.table(TABLE).upsert({'base_date': last, 'call_vol': call_vol, 'put_vol': put_vol},
                                   on_conflict='base_date').execute()
            log.info("[피어앤그리드] %s 옵션 콜 %d · 풋 %d (풋/콜 %.2f)",
                     last, call_vol, put_vol, put_vol / call_vol)
    else:
        log.info("[피어앤그리드] 오늘(%s)은 거래일 아님 — 옵션 거래량 건너뜀 (최신 %s)", today, last)
    return n


def backfill(start_yyyymmdd: str) -> int:
    """start일부터 지수·bond5 백필 (옵션 거래량은 과거 API가 없어 seed_options로)"""
    sb = get_supabase_client()
    rows = _fetch_indexes(start_yyyymmdd, _kst_today().strftime('%Y%m%d'))
    if not rows:
        log.warning("[피어앤그리드 백필] 지수 수집 결과 없음")
        return 0
    _chain_bond5(rows, _known_bond5(sb, '2000-01-01'))
    n = _upsert_indexes(sb, rows)
    log.info("[피어앤그리드 백필] %s~%s %d일 upsert", min(rows), max(rows), n)
    return n


def seed_options(path: str) -> int:
    """원본 엑셀에서 뽑은 {"YYYY-MM-DD": [call, put]} 로 과거 옵션 거래량 시드"""
    sb = get_supabase_client()
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    payload = [{'base_date': d, 'call_vol': int(c), 'put_vol': int(p)}
               for d, (c, p) in sorted(data.items()) if c and p]
    for i in range(0, len(payload), 500):
        sb.table(TABLE).upsert(payload[i:i + 500], on_conflict='base_date').execute()
    log.info("[피어앤그리드 시드] 옵션 거래량 %d일 upsert", len(payload))
    return len(payload)


if __name__ == '__main__':
    if '--backfill' in sys.argv:
        i = sys.argv.index('--backfill')
        backfill(sys.argv[i + 1] if len(sys.argv) > i + 1 else '20250401')
    elif '--seed-options' in sys.argv:
        seed_options(sys.argv[sys.argv.index('--seed-options') + 1])
    else:
        run()
