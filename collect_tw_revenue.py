"""대만 월별 매출 → tw_monthly_revenue · tw_revenue_universe (AI·반도체 공급망 관심 종목)

[원본] 태린이아빠 Market Dashboard '대만 월별 매출'(공개 소스 app.py update_taiwan_revenue · TW_AI_UNIVERSE).
  · 종목 = 밸류체인 26개(파운드리 … IC 유통). 한 종목이 두 밸류체인에 들어가기도 한다(3413 · 3583 · 6196 · 3131 · 6640).
  · 자료 = 대만 공개정보관측소(MOPS) 월매출 HTML — 열쇠 없이 받는다.
      https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{민국연도}_{월}_{0|1}.html   (sii 상장 · otc 장외, 0 국내 · 1 외국 기업, big5)
      열 순서: 公司代號 · 公司名稱 · 當月營收 · 上月營收 · 去年當月營收 · 上月比較增減(%) · 去年同月增減(%) · 累計 …   (단위 천 대만달러)
    최신 달은 공개 API도 함께 받아 늦은 신고를 채운다(같은 달이면 API 값이 이긴다 — 원본과 같음).
      https://openapi.twse.com.tw/v1/opendata/t187ap05_L · https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O
  · 보관 = 올해를 포함한 최근 4개 연도(예: 2026년이면 2023-01부터). 그 앞은 지운다.
  · 평소엔 저장된 마지막 달에서 2달 앞부터 이번 달까지만 다시 받는다. 표가 비었거나 종목이 바뀌면 4개 연도 전부.
  · 원본은 받지 못한 파일을 조용히 건너뛴다(그 달이 통째로 빈다). 여기서는 파일마다 3번까지 다시 받고,
    종목 수가 뭉텅 빠진 달은 다음 실행 때 다시 받으며, 끝내 못 받으면 실패로 알린다(2026-11-12 첫 전체 받기에서 2개 파일이 빠졌던 것).

[저장] revenue = 천 대만달러(원자료 그대로, 화면이 ÷1000 해서 백만 대만달러로 보인다), mom · yoy = 공식 증감률(%).
  공식 증감률이 빈 달은 화면이 저장된 매출로 계산한다. 값이 다른 줄만 쓴다.
  밸류체인·종목 이름은 tw_revenue_universe에 그대로 올려 화면이 읽는다(종목을 고칠 곳은 이 파일 한 곳).

[시각] 매일 21:10 — 대만 기업은 매달 10일까지 전달 매출을 신고한다(대만 시각 저녁 신고분까지 반영). 평소 약 1분, 전체 백필 약 12분.

실행: python3 collect_tw_revenue.py [--full] [--dry] [--months N] [--dump 경로]
"""

import re
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = get_logger(__name__)

TABLE = 'tw_monthly_revenue'
UNI_TABLE = 'tw_revenue_universe'
MOPS_HOSTS = ('https://mopsov.twse.com.tw', 'https://mops.twse.com.tw')
OPENAPI = (('TWSE', 'https://openapi.twse.com.tw/v1/opendata/t187ap05_L'),
           ('TPEX', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O'))
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; BatiInvest/1.0)'}
YEARS = 4                 # 올해 포함 보관 연도 수 (원본 rolling_4_calendar_years_v1)
RECENT_BACK = 2           # 평소 다시 받는 범위 = 저장된 마지막 달 − 2달부터
PAUSE = 0.4
TRIES = 3                 # 파일 하나를 다시 받는 횟수
HOLE_RATIO = 0.9          # 한 달의 종목 수가 가장 많은 달의 이 비율보다 적으면 빠진 달로 보고 다시 받는다

# 원본 TW_AI_UNIVERSE — 순서 그대로
UNIVERSE = {
    '파운드리': {'2330': 'TSMC', '2303': 'UMC', '5347': 'VIS', '6770': 'PSMC'},
    '소재·케미컬·포토마스크': {'2338': 'Taiwan Mask', '4755': 'San Fu Chemical', '4749': 'AEMC',
                               '4768': 'Crystalwise', '4770': 'ASC', '1727': 'Chung Hwa Chemical'},
    '실리콘 웨이퍼': {'6488': 'GlobalWafers', '5483': 'SAS', '6182': 'Wafer Works', '3532': 'Formosa Sumco', '6640': 'EPISIL'},
    '전공정 장비·소모품': {'3413': 'Foxsemicon', '3680': 'Gudeng', '3583': 'Scientech', '3131': 'GPTC'},
    '팹 건설·클린룸': {'2404': 'UIS', '5536': 'ACTER', '6139': 'L&K Engineering', '6196': 'Marketech'},
    'ASIC / IC설계': {'2454': 'MediaTek', '3661': 'Alchip', '3443': 'Global Unichip', '3035': 'Faraday',
                      '2379': 'Realtek', '3034': 'Novatek', '6531': 'Airoha', '4961': 'Fitipower'},
    'IP·고속 인터페이스 IC': {'3529': 'eMemory', '6643': 'M31', '4966': 'Parade', '5269': 'ASMedia', '3227': 'PixArt'},
    '아날로그·전력 IC': {'6286': 'Richtek', '6415': 'Silergy', '8081': 'GMT'},
    '메모리 & 유통': {'2408': 'Nanya Tech', '2344': 'Winbond', '2337': 'Macronix', '8299': 'Phison',
                      '3260': 'ADATA', '2451': 'Transcend', '5351': 'Etron'},
    '화합물 반도체 (RF/SiC/GaN)': {'3105': 'WIN Semi', '2455': 'VPEC', '8086': 'AWSC', '4971': 'IET-KY'},
    'OSAT (후공정)': {'3711': 'ASE', '6239': 'PTI', '8150': 'ChipMOS', '2449': 'KYEC',
                      '6257': 'Sigurd', '3264': 'Ardentec', '6147': 'Chipbond'},
    '후공정(CoWoS)·테스트 장비': {'3413': 'Foxsemicon', '3583': 'Scientech', '6196': 'Marketech', '3131': 'GPTC',
                                  '6187': 'All Ring', '6640': 'EPISIL', '5443': 'GPM'},
    '테스트 인터페이스': {'6515': 'WinWay', '6223': 'MPI', '6510': 'CHPT'},
    'PCB / 기판': {'3037': 'Unimicron', '3189': 'Kinsus', '8046': 'Nan Ya PCB', '4958': 'Zhen Ding',
                   '2313': 'Compeq', '2367': 'Unitech PCB', '8021': 'Topoint'},
    'CCL·동박': {'2383': 'Elite Material', '6213': 'ITEQ', '6274': 'TUC', '8358': 'Co-Tech'},
    'MLCC': {'2327': 'Yageo', '2492': 'Walsin Tech', '3026': 'Holy Stone', '6173': 'PDC'},
    '광통신 / CPO': {'3081': 'LandMark', '3363': 'FOCI', '3163': 'Browave', '4979': 'LuxNet',
                     '3450': 'Elaser', '6442': 'EZconn', '4908': 'APAC Opto'},
    '네트워크 장비': {'2345': 'Accton', '5388': 'Sercomm', '3596': 'Arcadyan', '6285': 'WNC',
                      '3380': 'Alpha Networks', '3704': 'Zyxel'},
    '광학렌즈': {'3008': 'Largan', '3406': 'GSEO'},
    'ODM / AI서버': {'2317': 'Hon Hai', '2382': 'Quanta', '3231': 'Wistron', '6669': 'Wiwynn', '2356': 'Inventec'},
    '전력·파워서플라이·BBU': {'2308': 'Delta', '6282': 'AcBel', '2301': 'Lite-On', '6412': 'Chicony Power',
                              '6781': 'AES-KY', '3211': 'Dynapack', '6121': 'Simplo', '6409': 'Voltronic'},
    '냉각': {'3324': 'Auras', '3017': 'AVC', '2421': 'Sunonwealth', '3653': 'Jentech',
             '8996': 'Kaori', '6230': 'Nidec Chaun-Choung'},
    '커넥터·케이블': {'3533': 'LOTES', '3665': 'BizLink-KY', '2392': 'Cheng Uei', '4980': 'ACON'},
    '서버 섀시·레일': {'8210': 'Chenbro', '2059': 'King Slide', '3693': 'AIC', '6117': 'In Win'},
    'BMC': {'5274': 'ASPEED', '4919': 'Nuvoton'},
    'IC 유통': {'3702': 'WPG', '3036': 'WT Micro', '8112': 'Supreme'},
}
TICKERS = {t for members in UNIVERSE.values() for t in members}

_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.I | re.S)
_CELL_RE = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.I | re.S)
_TAG_RE = re.compile(r'<[^>]+>')


def _clean_num(v):
    """원본 _clean_num — 쉼표·%를 떼고 숫자가 아니면 None."""
    if v is None:
        return None
    s = str(v).replace(',', '').replace('%', '').replace('−', '-').replace('&nbsp;', '').strip()
    if s in ('', '-', '--', 'nan', 'None'):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _has_schema(sb) -> bool:
    try:
        sb.table(TABLE).select('ticker').limit(1).execute()
        sb.table(UNI_TABLE).select('ticker').limit(1).execute()
        return True
    except Exception:
        return False


def parse_mops(html: str, ym: str, market: str) -> dict:
    """MOPS 월매출 HTML → {ticker: row}. 첫 칸이 관심 종목 코드인 줄만(위치 기반 — 원본과 같음)."""
    out = {}
    for m in _ROW_RE.finditer(html):
        cells = [_TAG_RE.sub('', c).strip() for c in _CELL_RE.findall(m.group(1))]
        if len(cells) < 7:
            continue
        code = re.search(r'\d{4,6}', cells[0])
        if not code or code.group(0) not in TICKERS:
            continue
        rev = _clean_num(cells[2])
        out[code.group(0)] = {'ticker': code.group(0), 'ym': ym, 'market': market,
                              'revenue': None if rev is None else int(round(rev)),
                              'mom': _clean_num(cells[5]), 'yoy': _clean_num(cells[6])}
    return out


def _get_mops(path: str):
    """MOPS 파일 하나 → (본문 | None, 실패 여부). 없는 파일(404·빈 응답)은 실패가 아니다. 시간 초과·서버 오류는 3번까지 다시 받는다."""
    import requests

    for attempt in range(TRIES):
        missing = 0
        for host in MOPS_HOSTS:
            try:
                r = requests.get(host + path, headers=HEADERS, timeout=25)
                if r.status_code == 200 and len(r.content) > 500:
                    r.encoding = 'big5'
                    return r.text, False
                if r.status_code in (200, 404):
                    missing += 1                # 아직 안 나온 달·없는 파일
            except Exception:
                pass
        if missing == len(MOPS_HOSTS):
            return None, False
        time.sleep(2.0 * (attempt + 1))
    return None, True


def fetch_mops_month(year: int, month: int, market: str) -> tuple:
    """한 달 · 한 시장(TWSE | TPEX) — 국내(0)·외국(1) 기업 파일 둘 다 → ({ticker: row}, [받지 못한 파일])."""
    kind = 'sii' if market == 'TWSE' else 'otc'
    out, failed = {}, []
    for flag in (0, 1):
        path = f'/nas/t21/{kind}/t21sc03_{year - 1911}_{month}_{flag}.html'
        txt, bad = _get_mops(path)
        if bad:
            failed.append(path.rsplit('/', 2)[-2] + '/' + path.rsplit('/', 1)[-1])
        if txt:
            out.update(parse_mops(txt, f'{year:04d}-{month:02d}-01', market))
        time.sleep(PAUSE)
    return out, failed


def fetch_openapi() -> dict:
    """최신 달 공개 API → {(ticker, ym): row}. 늦은 신고 보완용."""
    import requests

    out = {}
    for market, url in OPENAPI:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning(f'[대만월매출] 공개 API {market} 실패 — MOPS 값만 씀: {e}')
            continue

        for x in data if isinstance(data, list) else []:
            def pick(words):
                for k, v in x.items():
                    if any(w in str(k) for w in words):
                        return v
                return None
            code = re.search(r'\d{4,6}', str(pick(['公司代號']) or ''))
            if not code or code.group(0) not in TICKERS:
                continue
            ymn = re.sub(r'\D', '', str(pick(['資料年月']) or ''))
            if len(ymn) < 5:
                continue
            ym = f'{int(ymn[:-2]) + 1911:04d}-{int(ymn[-2:]):02d}-01'
            rev = _clean_num(pick(['當月營收']))
            out[(code.group(0), ym)] = {'ticker': code.group(0), 'ym': ym, 'market': market,
                                        'revenue': None if rev is None else int(round(rev)),
                                        'mom': _clean_num(pick(['上月比較增減'])), 'yoy': _clean_num(pick(['去年同月增減']))}
    return out


def universe_rows() -> list:
    return [{'category': cat, 'cat_ord': ci, 'ord': ti, 'ticker': t, 'name': name}
            for ci, (cat, members) in enumerate(UNIVERSE.items())
            for ti, (t, name) in enumerate(members.items())]


def _months(start: tuple, end: tuple) -> list:
    y, m = start
    out = []
    while (y, m) <= end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 1e-6


def run(full: bool = False, dry: bool = False, recent: int = None) -> dict:
    """recent = 시험용 — 이번 달에서 N달 앞부터만 받는다(저장된 범위와 무관)."""
    sb = get_supabase_client()
    has = _has_schema(sb)
    if not dry and not has:
        log.warning(f'[대만월매출] {TABLE} 테이블 없음 — sql/tw_revenue.sql을 먼저 실행하세요 (건너뜀)')
        return {'months': 0, 'rows': 0, 'written': 0, 'skipped': 'no_table'}

    now = datetime.now(timezone.utc) + timedelta(hours=9)
    first = (now.year - (YEARS - 1), 1)
    first_iso = f'{first[0]:04d}-01-01'
    have, uni_have = {}, []
    if has:
        have = {(r['ticker'], r['ym']): r for r in fetch_all_pages(
            sb.table(TABLE).select('ticker,ym,market,revenue,mom,yoy').order('ticker').order('ym'))}
        uni_have = sb.table(UNI_TABLE).select('category,cat_ord,ord,ticker,name').execute().data or []
    uni = universe_rows()
    key = lambda r: (r['category'], r['cat_ord'], r['ord'], r['ticker'], r['name'])
    uni_changed = sorted(map(key, uni_have)) != sorted(map(key, uni))
    have_tickers = {t for t, _ in have}

    # 표가 비었거나 종목이 바뀌면 4개 연도 전부, 아니면 마지막 달 − 2달부터
    if recent is not None:
        y, m = now.year, now.month
        for _ in range(recent):
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        start, full = max((y, m), first), False
    elif full or not have or not TICKERS <= have_tickers:
        start, full = first, True
    else:
        last = max(ym for _, ym in have)
        y, m = int(last[:4]), int(last[5:7])
        for _ in range(RECENT_BACK):
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        start = max((y, m), first)

    got, failed = {}, []
    months = _months(start, (now.year, now.month))
    # 예전에 파일을 못 받아 종목이 뭉텅 빠진 달도 함께 다시 받는다
    per = {}
    for _, ym in have:
        per[ym] = per.get(ym, 0) + 1
    if per:
        top = max(per.values())
        last_have = max(per)
        holes = [(int(ym[:4]), int(ym[5:7])) for ym in sorted(per)
                 if ym >= first_iso and ym != last_have and per[ym] < top * HOLE_RATIO]
        holes += [mm for mm in _months(first, (int(last_have[:4]), int(last_have[5:7])))
                  if f'{mm[0]:04d}-{mm[1]:02d}-01' not in per]
        months = sorted(set(months) | set(holes))
    for y, m in months:
        for market in ('TWSE', 'TPEX'):
            rows_m, bad = fetch_mops_month(y, m, market)
            failed += bad
            for t, row in rows_m.items():
                got[(t, row['ym'])] = row
    got.update(fetch_openapi())          # 같은 달이면 공개 API 값이 이긴다(원본 keep='last')
    got = {k: r for k, r in got.items() if r['ym'] >= first_iso and r['revenue'] is not None}
    if not got:
        raise RuntimeError('대만 MOPS에서 관심 종목의 월매출을 가져오지 못했습니다.')

    todo = [r for k, r in got.items()
            if k not in have or any(not _same(r[c], have[k].get(c)) for c in ('revenue', 'mom', 'yoy'))]
    latest = max(ym for _, ym in got)
    n_latest = sum(1 for _, ym in got if ym == latest)
    if not dry:
        if uni_changed:                  # 종목 목록은 통째로 바꾼다
            sb.table(UNI_TABLE).delete().neq('ticker', '').execute()
            sb.table(UNI_TABLE).insert(uni).execute()
        for i in range(0, len(todo), 500):
            sb.table(TABLE).upsert(todo[i:i + 500], on_conflict='ticker,ym').execute()
        sb.table(TABLE).delete().lt('ym', first_iso).execute()      # 4개 연도 밖은 지운다
    log.info(f"[대만월매출] {'dry-run ' if dry else ''}{'전체 ' if full else ''}{len(months)}개월 받음 · 최신 {latest[:7]} "
             f"{n_latest}/{len(TICKERS)}종목 · 받은 {len(got)}줄 · 쓴 {len(todo)}줄{' · 종목 목록 갱신' if uni_changed else ''}"
             f"{' · 못 받은 파일 ' + ', '.join(failed) if failed else ''}")
    if failed and not dry:
        # 받은 만큼은 이미 썼다. 못 받은 파일이 있으면 실패로 알려 재시도 잡이 다시 돌게 한다(다음 실행이 빠진 달을 채운다).
        raise RuntimeError(f"MOPS 파일 {len(failed)}개를 받지 못했습니다: {', '.join(failed[:6])}")
    return {'months': len(months), 'rows': len(got), 'written': 0 if dry else len(todo),
            'latest': latest, 'latest_count': n_latest, 'full': full, 'failed': failed, 'got': got if dry else None}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--full', action='store_true', help='최근 4개 연도 전부 다시 받기')
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 받기만')
    ap.add_argument('--months', type=int, help='시험용 — 이번 달에서 N달 앞부터만 받기')
    ap.add_argument('--dump', help='--dry로 받은 줄을 이 경로에 JSON으로 저장(종목 목록 포함)')
    a = ap.parse_args()
    r = run(full=a.full, dry=a.dry, recent=a.months)
    if a.dump and r.get('got'):
        import json
        with open(a.dump, 'w', encoding='utf-8') as f:
            json.dump({'rows': list(r['got'].values()), 'universe': universe_rows()}, f, ensure_ascii=False)
    print({k: v for k, v in r.items() if k != 'got'})
