"""
collect_exports.py
──────────────────
관세청 공공데이터 API → export_monthly (화면: 프론트 js/export-data.js '수출 데이터' 페이지)

[원본] 태린이아빠 「주요품목별수출정리」 — 목록·원본과 다른 점은 export_items.py 머리말.
  원작자는 TRASS에서 손으로 조회해 붙여넣는다. 여기서는 같은 계열을 관세청 API로 받는다.
  일평균·판가·MoM·YoY·분기 계산은 화면이 한다 — 여기서는 월별 원자료만 쌓는다.

[출처] data.go.kr 관세청 (서버 .env DATA_GO_KR_KEY = 일반 인증키 Decoding, 사용자가 직접 넣음)
  · 품목별 수출입실적(GW)       Itemtrade   — HS 2/4/6/10, 금액·중량(kg)     → lvl nat
  · 품목별 국가별 수출입실적(GW) nitemtrade  — 위 + 국가                       → lvl cnty
  · 시군구별 품목별 수출입실적   sigunguperprlstperacrs — HS 6자리 × 시도 → 시군구별 금액(중량 없음) → lvl sgg
  매월 15일경 전월분 확정치가 올라오고 정정·취하가 반영된다 → 매일 최근 3개월을 다시 받는다(멱등 upsert).
  조회 기간은 한 번에 1년 이내.

실행:
  python3 collect_exports.py                       # 최근 3개월 (일일 잡)
  python3 collect_exports.py --backfill 2022-01    # 그 달부터 지난달까지
  python3 collect_exports.py --only p002,f003      # 몇 개만 (점검용)
  python3 collect_exports.py --seed-wb FILE.json   # 원본 워크북 기록(공장 시트 과거 중량) → export_wb
"""
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages
import export_items as EI

log = get_logger(__name__)

BASE = 'https://apis.data.go.kr/1220000'
PATH = {
    'nat': '/Itemtrade/getItemtradeList',
    'cnty': '/nitemtrade/getNitemtradeList',
    'sgg': '/sigunguperprlstperacrs/getSigunguPerPrlstPerAcrs',
}
PAUSE = 0.15          # 요청 사이(초)
RECENT_MONTHS = 3     # 일일 잡이 다시 받는 기간
CAL_TABLE = 'fear_greed_daily'   # 거래일 달력 (영업일수)
_YM = re.compile(r'(\d{4})\D{0,3}(\d{1,2})')


def _kst_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def _ym_add(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f'{y:04d}-{m:02d}'


def _windows(start: str, end: str):
    """[start, end]를 12개월 이하 구간으로 — API는 조회 기간 1년 이내."""
    s = start
    while s <= end:
        e = min(_ym_add(s, 11), end)
        yield s, e
        s = _ym_add(e, 1)


def _num(v):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return None


class Customs:
    """data.go.kr 관세청 API 호출 (XML)."""

    def __init__(self):
        self.key = os.environ.get('DATA_GO_KR_KEY')
        if not self.key:
            raise RuntimeError('.env에 DATA_GO_KR_KEY(공공데이터포털 일반 인증키)가 없습니다')
        self.s = requests.Session()
        self.calls = 0

    def items(self, lvl: str, **params) -> list:
        q = {'serviceKey': self.key, 'numOfRows': 10000, 'pageNo': 1, **params}
        for attempt in range(3):
            try:
                r = self.s.get(BASE + PATH[lvl], params=q, timeout=40)
                self.calls += 1
                time.sleep(PAUSE)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                break
            except (requests.RequestException, ET.ParseError) as e:
                if attempt == 2:
                    raise
                log.warning(f'[수출] {lvl} {params} 재시도 {attempt + 1}: {e}')
                time.sleep(2 * (attempt + 1))
        code = root.findtext('.//resultCode') or root.findtext('.//returnReasonCode')
        if code and code not in ('00', '0', '000'):
            msg = root.findtext('.//resultMsg') or root.findtext('.//returnAuthMsg') or root.findtext('.//errMsg')
            raise RuntimeError(f'관세청 API {lvl} 오류 {code}: {msg}')
        rows = [{c.tag: (c.text or '').strip() for c in it} for it in root.iter('item')]
        total = _num(root.findtext('.//totalCount'))
        if total and total > len(rows):
            log.warning(f'[수출] {lvl} {params} 결과 {len(rows)}/{int(total)}행 — 페이지가 잘림')
        return rows


def _ym_of(text: str):
    m = _YM.search(text or '')
    return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}' if m else None


def _by_month_code(rows: list, hs: str, code_key: str, fields: dict) -> dict:
    """품목 API 행 → {ym: {필드: 합}}. 요청 코드와 같은 행이 있으면 그 행만, 없으면 하위 코드 행을 더한다."""
    exact = [r for r in rows if (r.get(code_key) or '') == hs]
    use = exact or [r for r in rows if (r.get(code_key) or '').startswith(hs)]
    out = {}
    for r in use:
        ym = _ym_of(r.get('year'))
        if not ym:   # '총계' 행 등
            continue
        d = out.setdefault(ym, {k: None for k in fields})
        for k, src in fields.items():
            v = _num(r.get(src))
            if v is not None:
                d[k] = (d[k] or 0) + v
    return out


NAT_FIELDS = {'exp_usd': 'expDlr', 'exp_wgt': 'expWgt', 'imp_usd': 'impDlr', 'imp_wgt': 'impWgt'}


def fetch_nat(api: Customs, hs: str, cn: str, start: str, end: str) -> dict:
    out = {}
    for s, e in _windows(start, end):
        p = {'strtYymm': s.replace('-', ''), 'endYymm': e.replace('-', ''), 'hsSgn': hs}
        if cn:
            p['cntyCd'] = cn
        rows = api.items('cnty' if cn else 'nat', **p)
        if cn:
            rows = [r for r in rows if (r.get('statCd') or cn) == cn]
        out.update(_by_month_code(rows, hs, 'hsCd' if cn else 'hsCode', NAT_FIELDS))
    return out


def _sgg_match(sgg: str, city: str) -> bool:
    """API 시군구명 ↔ 목록의 시군구 ('성남시'는 '성남시 분당구'도 포함)."""
    sgg = (sgg or '').strip()
    cands = [sgg]
    toks = sgg.split()
    if len(toks) > 1:
        cands.append(' '.join(toks[1:]))   # 앞에 시도 이름이 붙은 경우
    return any(c == city or c.startswith(city) for c in cands)


def fetch_sgg(api: Customs, hs6: str, sido: str, start: str, end: str) -> list:
    """6자리 × 시도 → [(ym, 시군구명, 수출액, 수입액)] — 시도 코드 후보를 모두 묻고 합친다."""
    seen, out = set(), []
    for code in EI.SIDO[sido]:
        for s, e in _windows(start, end):
            rows = api.items('sgg', strtYymm=s.replace('-', ''), endYymm=e.replace('-', ''), HsSgn=hs6, sidoCd=code)
            for r in rows:
                ym, nm = _ym_of(r.get('priodTitle')), (r.get('sggNm') or '').strip()
                if not ym or not nm or nm in ('총계', '합계', '소계', '계') or (ym, nm) in seen:
                    continue
                seen.add((ym, nm))
                out.append((ym, nm, _num(r.get('expUsdAmt')), _num(r.get('impUsdAmt'))))
    return out


def _region_at(rg: str, ym: str) -> str:
    ren = EI.REGION_RENAME.get(rg)
    return ren[1] if ren and ym >= ren[0] else rg


def _upsert(sb, table: str, rows: list, conflict: str):
    for i in range(0, len(rows), 500):
        sb.table(table).upsert(rows[i:i + 500], on_conflict=conflict).execute()


def _has_schema(sb) -> bool:
    try:
        sb.table('export_monthly').select('ym').limit(1).execute()
        sb.table('export_items').select('id').limit(1).execute()
        return True
    except Exception:
        return False


def sync_items(sb, items: list):
    _upsert(sb, 'export_items', [{
        'id': it['id'], 'tab': it['tab'], 'ord': it['ord'], 'name': it['name'], 'co': it['co'], 'note': it['note'],
        'flow': it['flow'], 'parts': it['parts'], 'orig': it['orig'], 'orig_last': it['orig_last'],
        'updated_at': datetime.now(timezone.utc).isoformat()} for it in items], 'id')
    have = {r['id'] for r in fetch_all_pages(sb.table('export_items').select('id'))}
    gone = sorted(have - {it['id'] for it in items})
    if gone:
        sb.table('export_items').delete().in_('id', gone).execute()


def sync_workdays(sb, upto: str):
    """완결된 달의 영업일수 = 거래일 달력(fear_greed_daily) 날짜 수. 이미 있는 달(원본 다수 분모)은 두고 빈 달만 채운다."""
    have = {r['ym'] for r in fetch_all_pages(sb.table('export_workdays').select('ym'))}
    last = max(have) if have else '2021-12'
    rows = []
    ym = _ym_add(last, 1)
    while ym <= upto:
        start, end = f'{ym}-01', f'{_ym_add(ym, 1)}-01'
        days = fetch_all_pages(sb.table(CAL_TABLE).select('base_date').gte('base_date', start).lt('base_date', end))
        if not days:
            break
        rows.append({'ym': ym, 'days': len(days), 'src': 'cal'})
        ym = _ym_add(ym, 1)
    if rows:
        _upsert(sb, 'export_workdays', rows, 'ym')
        log.info(f'[수출] 영업일수 추가 {[(r["ym"], r["days"]) for r in rows]}')


def run(start: str = None, only: set = None) -> dict:
    """start(YYYY-MM)부터 지난달까지 목록의 모든 계열을 받아 upsert. 반환 {'series': n, 'rows': n, 'calls': n}."""
    sb = get_supabase_client()
    if not _has_schema(sb):
        log.warning('[수출] sql/export_data.sql 미실행 — 건너뜀')
        return {}
    if not os.environ.get('DATA_GO_KR_KEY'):
        log.warning('[수출] .env DATA_GO_KR_KEY 없음 — 건너뜀 (공공데이터포털 키 발급 후 서버에 넣기)')
        return {}
    api = Customs()
    today = _kst_today()
    end = _ym_add(today.strftime('%Y-%m'), -1)
    start = start or _ym_add(end, -(RECENT_MONTHS - 1))
    items = EI.items()
    if not only:
        sync_items(sb, items)
    else:
        items = [it for it in items if it['id'] in only]

    # 계열 모으기 — 같은 계열을 여러 항목이 쓰면 한 번만 받는다
    nat, sgg = set(), {}
    for it in items:
        for p in it['parts']:
            lv = EI.lvl_of(p)
            if lv in ('nat', 'cnty'):
                nat.add((p['hs'], p['cn']))
            elif lv == 'sgg':
                sido = p['rg'].split()[0]
                for h6 in EI.sgg_hs6(p['hs']):
                    sgg.setdefault((h6, sido), set()).add(p['rg'])
            # total(총수출·국가 총액)은 아직 받지 않는다 — 품목 API로는 합계를 바로 못 받음

    now = datetime.now(timezone.utc).isoformat()
    n_rows, fails = 0, []
    for hs, cn in sorted(nat):
        try:
            data = fetch_nat(api, hs, cn, start, end)
        except Exception as e:
            fails.append(f'{hs}@{cn}: {e}')
            log.error(f'[수출] {hs}@{cn} 실패: {e}')
            continue
        rows = [{'lvl': 'cnty' if cn else 'nat', 'hs': hs, 'area': cn, 'ym': ym, **v, 'updated_at': now}
                for ym, v in sorted(data.items())]
        if rows:
            _upsert(sb, 'export_monthly', rows, 'lvl,hs,area,ym')
            n_rows += len(rows)

    # 시군구: 목록 부분 = (원본 HS 4·6·10자리, 지역) → 6자리 하위 합 × 지역 이름이 맞는 시군구 합
    acc = {}   # (part_hs, rg, ym) → [exp, imp]
    parts_of = {}   # (h6, sido) → [(part_hs, rg)]
    for it in items:
        for p in it['parts']:
            if EI.lvl_of(p) == 'sgg':
                for h6 in EI.sgg_hs6(p['hs']):
                    parts_of.setdefault((h6, p['rg'].split()[0]), set()).add((p['hs'], p['rg']))
    unmatched = set()
    for (h6, sido), regs in sorted(sgg.items()):
        try:
            rows = fetch_sgg(api, h6, sido, start, end)
        except Exception as e:
            fails.append(f'{h6}/{sido}: {e}')
            log.error(f'[수출] 시군구 {h6}/{sido} 실패: {e}')
            continue
        for part_hs, rg in parts_of[(h6, sido)]:
            hit = False
            for ym, nm, ex, im in rows:
                city = _region_at(rg, ym).split(maxsplit=1)[1]
                if _sgg_match(nm, city):
                    hit = True
                    a = acc.setdefault((part_hs, rg, ym), [None, None])
                    if ex is not None:
                        a[0] = (a[0] or 0) + ex
                    if im is not None:
                        a[1] = (a[1] or 0) + im
            if rows and not hit:
                unmatched.add((rg, h6, tuple(sorted({nm for _, nm, _, _ in rows}))[:8]))
    rows = [{'lvl': 'sgg', 'hs': hs, 'area': rg, 'ym': ym, 'exp_usd': v[0], 'exp_wgt': None,
             'imp_usd': v[1], 'imp_wgt': None, 'updated_at': now} for (hs, rg, ym), v in sorted(acc.items())]
    if rows:
        _upsert(sb, 'export_monthly', rows, 'lvl,hs,area,ym')
        n_rows += len(rows)
    for rg, h6, names in sorted(unmatched):
        log.info(f'[수출] 시군구 이름 불일치? {rg} ({h6}) — API 시군구 예: {list(names)}')

    try:
        sync_workdays(sb, end)
    except Exception as e:
        log.warning(f'[수출] 영업일수 갱신 실패: {e}')
    log.info(f'[수출] {start}~{end} 완료 — 전국·국가 {len(nat)}계열 · 시군구 {len(sgg)}쌍 · {n_rows}행 · API {api.calls}회'
             + (f' · 실패 {len(fails)}' if fails else ''))
    if fails:
        raise RuntimeError(f'{len(fails)}개 계열 실패: ' + ' | '.join(fails[:5]))
    return {'series': len(nat) + len(sgg), 'rows': n_rows, 'calls': api.calls}


def seed_wb(path: str):
    """원본 워크북 기록 → export_wb (관리자만 읽음). JSON {item_id: [[ym, usd, wgt], ...]}"""
    sb = get_supabase_client()
    data = json.load(open(path, encoding='utf-8'))
    rows = [{'item_id': iid, 'ym': ym, 'usd': usd, 'wgt': wgt} for iid, rs in data.items() for ym, usd, wgt in rs]
    _upsert(sb, 'export_wb', rows, 'item_id,ym')
    log.info(f'[수출] 원본 기록 {len(data)}개 항목 · {len(rows)}행')


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--seed-wb' in args:
        seed_wb(args[args.index('--seed-wb') + 1])
    else:
        st = args[args.index('--backfill') + 1] if '--backfill' in args else None
        only = set(args[args.index('--only') + 1].split(',')) if '--only' in args else None
        print(run(st, only))
