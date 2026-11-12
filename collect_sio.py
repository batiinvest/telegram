"""SIO — 종목군별 시장 내부 강도(과매도·과매수) → sio_daily

[원본] 태린이아빠 Market Dashboard '한국 과매도 과매수 점검'(공개 소스 app.py _sio_calculate).
  코스피200 · 코스닥150 · 코스피대형주 구성종목을 그날 오른 종목과 내린 종목으로 나눠
    상승평균 = (상승 종목 거래량 합 ÷ 전체 거래량 + 상승 종목 등락률 합 ÷ 전체 등락률 절댓값 합) ÷ 2
    하락평균 = 같은 식을 내린 종목으로
    SIO = 상승평균이 크면 +상승평균, 아니면 −하락평균        (−1 ~ +1)
  등락 = 종가 ÷ 기준가 − 1(기준가 = 종가 − 전일 대비), 거래량 0·보합 종목은 뺀다. 등락률 합은 가중 없이 더한다.
  판정(화면 js/sio.js): ≤ −80% 과매도 · ≥ +80% 과매수 · ≥ +90% 강한 과매수 ·
    과매도 뒤 1~5거래일 안에 +80% 이상 = Bottom 통과.

[자료] 구성종목 = KRX '지수구성종목'(MDCSTAT00601, 날짜 지정, 로그인 — collect_active_etf.Krx).
  시세·거래량 = market_data(price · price_change · volume, 15:45 장 마감 수집).
  원본은 엑셀에 붙여 넣은 구성종목·일별 시세로 계산한다.

[원본 대조 — 2026-11-12, 원본 화면 10-21 ~ 11-11 16거래일 × 3개 군 = 48칸]
  평균 오차 0.12%p · 최대 1.38%p(10-29, 세 군 모두 원본 쪽이 1%p쯤 낮음) · 0.5%p 이내 45칸.
  과매도·과매수 판정은 47칸 같고 1칸(10-29 코스닥150 원본 −80.2% vs −79.5%)만 경계에서 갈린다.
  KRX 전종목 시세(시간외 포함 거래량)로 계산하면 평균 0.21%p로 더 벌어진다 — 원본도 정규장 마감 직후(15:51) 자료다.

[시각] 16:02(15:45 전 종목 마감 수집이 15:55쯤 끝난 뒤) · 18:46(수집값이 고쳐졌을 때 반영, 같은 값이면 그대로).
  표가 비어 있으면 market_data에 남아 있는 날짜 전부를 채운다(보존 약 120일).

실행: python3 collect_sio.py [--days N] [--dry]
"""

import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = get_logger(__name__)

BLD = 'dbms/MDC/STAT/standard/MDCSTAT00601'
PAUSE = 0.3
# (저장 키, 이름, KRX indIdx, indIdx2) — 원본 _SIO_GROUP_ORDER 순서
GROUPS = [
    ('k200',   '코스피200',    '1', '028'),
    ('kq150',  '코스닥150',    '2', '203'),
    ('klarge', '코스피대형주', '1', '002'),
]
PROBE = '005930'      # 거래일 목록용 — 늘 시세가 있는 종목
MIN_COVER = 0.95      # 구성종목 중 그날 시세가 있는 비율이 이보다 낮으면 계산하지 않는다
RECENT = 10           # 매일 다시 보는 최근 거래일 수(빠진 날 채움)
EPS = 1e-12


def sio_calc(members: list, rec: dict) -> dict:
    """원본 _sio_calculate 한 군·하루. rec: {종목코드: (거래량, 등락 비율)} (DB·네트워크 없음).

    반환 {sio, up_avg, down_avg, up_cnt, down_cnt, n, up_vol, up_pt} — 계산 못 하면 sio=None.
    """
    up_vol = down_vol = up_pt = down_pt = 0.0
    up_cnt = down_cnt = 0
    for code in members:
        r = rec.get(code)
        if not r:
            continue
        vol, chg = r
        if vol is None or vol <= 0 or chg is None:
            continue
        if chg > EPS:
            up_vol += vol; up_pt += chg; up_cnt += 1
        elif chg < -EPS:
            down_vol += vol; down_pt -= chg; down_cnt += 1
    tot_vol, tot_pt = up_vol + down_vol, up_pt + down_pt
    out = {'sio': None, 'up_avg': None, 'down_avg': None, 'up_cnt': up_cnt, 'down_cnt': down_cnt,
           'n': len(members), 'up_vol': None, 'up_pt': None}
    if not tot_vol or not tot_pt:
        return out
    up_avg = (up_vol / tot_vol + up_pt / tot_pt) / 2.0
    down_avg = (down_vol / tot_vol + down_pt / tot_pt) / 2.0
    out.update(sio=up_avg if up_avg > down_avg else -down_avg, up_avg=up_avg, down_avg=down_avg,
               up_vol=up_vol / tot_vol, up_pt=up_pt / tot_pt)
    return out


def _has_schema(sb) -> bool:
    try:
        sb.table('sio_daily').select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def _quotes(sb, day: str, codes: list) -> dict:
    """그날 {종목코드: (거래량, 종가 ÷ 기준가 − 1)}."""
    rec = {}
    for i in range(0, len(codes), 200):
        rows = sb.table('market_data').select('stock_code,price,price_change,volume') \
                 .eq('base_date', day).in_('stock_code', codes[i:i + 200]).execute().data or []
        for r in rows:
            p, ch = r.get('price'), r.get('price_change')
            if not p or ch is None or p - ch <= 0:
                continue
            rec[r['stock_code']] = (r.get('volume'), p / (p - ch) - 1)
    return rec


def compute_day(sb, krx, day: str) -> list:
    """하루치 3개 군 행. 구성종목 응답이 비거나 시세가 모자란 군은 뺀다."""
    ymd = day.replace('-', '')
    members = {}
    for key, _name, a, b in GROUPS:
        out = krx.get(BLD, indIdx=a, indIdx2=b, trdDd=ymd, money='3', csvxls_isNo='false').get('output') or []
        time.sleep(PAUSE)
        members[key] = [(r.get('ISU_SRT_CD') or '').strip().upper() for r in out if r.get('ISU_SRT_CD')]
    rec = _quotes(sb, day, sorted({c for m in members.values() for c in m}))
    rows = []
    for key, name, _a, _b in GROUPS:
        mem = members[key]
        have = sum(1 for c in mem if c in rec)
        if not mem or have < len(mem) * MIN_COVER:
            log.warning(f'[SIO] {day} {name} — 구성종목 {len(mem)} 중 시세 {have}개, 계산하지 않습니다')
            continue
        r = sio_calc(mem, rec)
        if r['sio'] is None:
            continue
        rows.append({'base_date': day, 'grp': key,
                     **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()}})
    return rows


def run(days: int = None, dry: bool = False) -> dict:
    """최근 거래일 중 빠진 날을 채우고 가장 최근 날은 늘 다시 계산한다."""
    from collect_active_etf import Krx

    sb = get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning('[SIO] sio_daily 테이블 없음 — sql/sio_daily.sql을 먼저 실행하세요 (건너뜀)')
        return {'dates': [], 'rows': 0, 'skipped': 'no_table'}
    have = set()
    if not dry or _has_schema(sb):
        have = {r['base_date'] for r in fetch_all_pages(
            sb.table('sio_daily').select('base_date').eq('grp', GROUPS[0][0]).order('base_date'))}
    if days is None:
        days = RECENT if have else 400      # 처음엔 남아 있는 날짜 전부
    cal = [r['base_date'] for r in sb.table('market_data').select('base_date').eq('stock_code', PROBE)
           .order('base_date', desc=True).limit(days).execute().data or []]
    if not cal:
        log.warning('[SIO] market_data 거래일을 찾지 못했습니다')
        return {'dates': [], 'rows': 0}
    # 장중엔 오늘 행이 모니터링 종목만 있다(전 종목은 15:45 수집) — 그때 손으로 돌리면 오늘은 건너뛴다
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    intraday = cal[0] == now.date().isoformat() and (now.hour, now.minute) < (15, 55)
    todo = sorted(d for i, d in enumerate(cal) if (i == 0 and not intraday) or (i > 0 and d not in have))

    krx = Krx()
    krx.login()
    done, total = [], 0
    for day in todo:
        rows = compute_day(sb, krx, day)
        if not rows:
            continue
        if not dry:
            sb.table('sio_daily').upsert(rows, on_conflict='base_date,grp').execute()
        done.append(day)
        total += len(rows)
        if day == cal[0] or dry:
            log.info(f'[SIO] {day} ' + ' · '.join(
                f"{next(n for k, n, _a, _b in GROUPS if k == r['grp'])} {r['sio'] * 100:+.1f}%"
                f"(상승 {r['up_cnt']}·하락 {r['down_cnt']})" for r in rows))
    if cal[0] not in done and not intraday:
        # 가장 최근 거래일을 못 구하면 실패로 알린다 — 지난 날 채움은 그대로 남는다
        raise RuntimeError(f'{cal[0]} SIO를 계산하지 못했습니다(구성종목·시세 확인)')
    if done:
        log.info(f"[SIO] {'dry-run ' if dry else ''}{len(done)}일 · {total}행 ({done[0]} ~ {done[-1]})")
    return {'dates': done, 'rows': total}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, help='다시 볼 최근 거래일 수 (기본: 최근 10일 중 빠진 날, 표가 비면 전부)')
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    a = ap.parse_args()
    r = run(days=a.days, dry=a.dry)
    print({**r, 'dates': f"{len(r['dates'])}일"})
