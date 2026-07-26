"""
collect_insider.py — DART 임원·주요주주 지분공시 파싱 & insider_trades 저장

변동사유 코드 (RPT_RSN AUNITVALUE):
  1=취득(+)  2=처분(-) 3=유상증자(+) 4=무상증자(+) 5=주식배당(+)
  6=전환권행사(+) 7=신주인수권행사(+) 8=교환권행사(+)
  9=합병(+) 10=분할(+/-) 11=상속(+) 12=증여(+/-) 13=수증(+)
  35=수증(+)  36=증여(-)  기타
"""

import requests, zipfile, io, logging, time
import xml.etree.ElementTree as ET
from datetime import date
from typing import Optional
from collect_utils import batch_upsert

log = logging.getLogger(__name__)

# 변동사유 코드 → (label, direction)
# direction: +1=취득, -1=처분, 0=기타
REASON_MAP = {
    '1':  ('취득',         +1),
    '2':  ('처분',         -1),
    '3':  ('유상증자',     +1),
    '4':  ('무상증자',     +1),
    '5':  ('주식배당',     +1),
    '6':  ('전환권행사',   +1),
    '7':  ('신주인수권행사', +1),
    '8':  ('교환권행사',   +1),
    '9':  ('합병',         +1),
    '10': ('분할',          0),
    '11': ('상속',         +1),
    '12': ('증여',         -1),
    '13': ('수증',         +1),
    '35': ('수증',         +1),
    '36': ('증여',         -1),
}

def _parse_int(s: str) -> Optional[int]:
    """'1,234,567' → 1234567"""
    if not s or s.strip() in ('-', ''):
        return None
    try:
        return int(s.replace(',', '').strip())
    except:
        return None


def _decode_xml(content_bytes: bytes) -> str:
    """ZIP에서 읽은 바이트를 XML 문자열로 디코딩 (인코딩 자동 감지)"""
    import re
    for enc in ('utf-8', 'euc-kr', 'cp949', 'utf-8-sig'):
        try:
            content = content_bytes.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        content = content_bytes.decode('utf-8', errors='ignore')

    # XML 특수문자 정규화
    # 1. & 처리 (이미 엔티티인 것 제외)
    content = re.sub(r'&(?!(amp|lt|gt|quot|apos|#\d+|#x[\da-fA-F]+);)', '&amp;', content)
    # 2. 텍스트 내 꺽쇠 처리 — XML 태그가 아닌 < > 를 엔티티로 변환
    #    실제 XML 태그: 영문 태그명 + 공백/속성 or 슬래시/물음표/느낌표
    #    비태그: <DB김준기문화재단>, <한국기업명> 등 공백없이 한글/특수문자 포함
    def escape_text_brackets(m):
        inner = m.group(1)
        # XML 태그 조건: 영문/언더스코어로 시작 + (속성 있거나 종료태그 /로 시작 or 선언 ? ! )
        if re.match(r'^[/!?]', inner):          # 종료태그, 선언
            return m.group(0)
        if re.match(r'^[A-Za-z_]', inner):
            # 공백이 있으면 속성 포함 태그 → 그대로
            if ' ' in inner or '\n' in inner or '\t' in inner:
                return m.group(0)
            # 순수 영문/숫자/하이픈/언더스코어만으로 구성된 태그명 → 그대로
            if re.match(r'^[A-Za-z_][A-Za-z0-9_\-]*$', inner):
                return m.group(0)
        # 나머지 (한글 포함, 특수문자 포함 등) → 이스케이프
        return f'&lt;{inner}&gt;'
    content = re.sub(r'<([^>]+)>', escape_text_brackets, content)
    # 3. 제어문자 제거 (XML 1.0 허용 범위 외)
    content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', content)
    return content

def parse_insider_xml(xml_content: str, rcept_no: str, corp_code: str,
                      corp_name: str, base_date: date) -> list[dict]:
    """
    임원·주요주주 소유상황보고서 XML → insider_trades rows 리스트 반환
    한 공시에 여러 변동행이 있을 수 있음
    """
    root = ET.fromstring(xml_content)
    rows = []

    # 보고자 이름 (IFR_NM)
    reporter = ''
    for tu in root.iter('TE'):
        if tu.get('ACODE') == 'IFR_NM':
            reporter = (tu.text or '').strip()
            break
    if not reporter:
        for tu in root.iter('TU'):
            if tu.get('AUNIT') == 'IFR_NM':
                reporter = (tu.text or '').strip()
                break

    # 보고자 구분 (등기임원/최대주주 등) — STF_RYN or MAIN_SH
    relation = ''
    for tu in root.iter('TU'):
        aunit = tu.get('AUNIT', '')
        if aunit == 'STF_RYN':
            relation = (tu.text or '').strip()
            break
        if aunit == 'MAIN_SH':
            relation = (tu.text or '').strip()

    # 종목코드 (CRP_CD)
    stock_code = ''
    for te in root.iter('TE'):
        if te.get('ACODE') == 'CRP_CD':
            stock_code = (te.text or '').strip().replace(',', '')
            break

    # 세부변동내역 테이블 파싱
    # TABLE-GROUP ACLASS="RPT_RSN" 하위 TR들
    in_detail = False
    for tg in root.iter('TABLE-GROUP'):
        if tg.get('ACLASS') == 'RPT_RSN':
            in_detail = True
            for tr in tg.iter('TR'):
                row_data = {}
                for child in tr:
                    aunit = child.get('AUNIT', '')
                    acode = child.get('ACODE', '')
                    val   = (child.text or '').strip()
                    uval  = child.get('AUNITVALUE', '')

                    if aunit == 'RPT_RSN':
                        row_data['reason_code'] = uval
                        row_data['reason']      = val
                    elif aunit == 'MDF_DM':
                        row_data['trade_date']  = uval  # YYYYMMDD
                    elif aunit == 'STR_KND':
                        row_data['stock_type']  = val
                    elif acode == 'BFR_STK_CNT':
                        row_data['shares_before'] = _parse_int(val)
                    elif acode == 'MDF_STK_CNT':
                        row_data['shares_change'] = _parse_int(val)
                    elif acode == 'AFR_STK_CNT':
                        row_data['shares_after']  = _parse_int(val)
                    elif acode == 'ACI_AMT2':
                        row_data['price'] = _parse_int(val)

                # reason_code 있으면 유효한 데이터 행
                if 'reason_code' not in row_data:
                    continue

                reason_code = row_data['reason_code']
                _, direction = REASON_MAP.get(reason_code, ('기타', 0))

                # 처분(-) 이면 증감을 음수로
                shares_change = row_data.get('shares_change')
                if shares_change and direction == -1:
                    shares_change = -abs(shares_change)

                # trade_date 파싱
                td_str = row_data.get('trade_date', '')
                try:
                    trade_date = date(int(td_str[:4]), int(td_str[4:6]), int(td_str[6:8])) if len(td_str) == 8 else None
                except:
                    trade_date = None

                rows.append({
                    'rcept_no':     rcept_no,
                    'corp_code':    corp_code,
                    'corp_name':    corp_name,
                    'stock_code':   stock_code,
                    'reporter':     reporter,
                    'relation':     relation,
                    'trade_date':   trade_date.isoformat() if trade_date else None,
                    'reason':       row_data.get('reason', ''),
                    'reason_code':  reason_code,
                    'stock_type':   row_data.get('stock_type', '보통주'),
                    'shares_before': row_data.get('shares_before'),
                    'shares_change': shares_change,
                    'shares_after':  row_data.get('shares_after'),
                    'price':         row_data.get('price'),
                    'base_date':     base_date.isoformat(),
                })

    return rows


def parse_bulk_holder_xml(xml_content: str, rcept_no: str, corp_code: str,
                          corp_name: str, base_date: date) -> Optional[dict]:
    """
    대량보유상황보고서 XML → 보유 변동 row 반환
    핵심 필드: BFR_STK_CNT(변동전), THS_STK_CNT(변동후), MDF_STK_CNT(증감)
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    def find_val(acode):
        for te in root.iter('TE'):
            if te.get('ACODE') == acode:
                return (te.text or '').strip()
        return None

    def find_unit(aunit):
        for tu in root.iter('TU'):
            if tu.get('AUNIT') == aunit:
                return tu.get('AUNITVALUE', ''), (tu.text or '').strip()
        return '', ''

    # 보고자
    reporter = find_val('IFR_NM') or find_val('RPT_RSP_NM') or ''

    # 보고 유형 (신규/변경/일부처분 등)
    rpt_dst_val, rpt_dst_txt = find_unit('RPT_DST1')

    # 보유 주식수 (변동 전/후/증감) - 합계 기준
    bfr = _parse_int(find_val('BFR_STK_CNT') or find_val('SUM_BMT_CNT') or '')
    ths = _parse_int(find_val('THS_STK_CNT') or find_val('SUM_TMT_CNT') or '')
    mdf = _parse_int(find_val('MDF_STK_CNT') or '')

    # 증감 계산 (mdf 없으면 후-전으로 계산)
    if mdf is None and bfr is not None and ths is not None:
        mdf = ths - bfr

    # mdf=0이고 보유비율도 같으면 담보계약 변경 등 실질 변동 없음
    # → shares_change=0으로 저장 (프론트에서 "변동없음" 표시)
    if mdf is None:
        mdf = 0

    # 보유 비율
    bfr_rt = find_val('BFR_STK_RT') or find_val('SUM_BMT_RT') or ''
    ths_rt = find_val('THS_STK_RT') or find_val('SUM_TMT_RT') or ''

    # 변동 방향 판단
    direction = 0
    if mdf is not None:
        direction = 1 if mdf > 0 else (-1 if mdf < 0 else 0)

    # 변동사유
    reason_text = find_val('CHN_RSN') or find_val('CHN_RSM') or rpt_dst_txt or ''

    return {
        'rcept_no':     rcept_no,
        'corp_code':    corp_code,
        'corp_name':    corp_name,
        'stock_code':   find_val('CRP_CD') or '',
        'reporter':     reporter,
        'relation':     find_val('FLT_CRP_RLT') or '',
        'trade_date':   base_date.isoformat(),
        'reason':       reason_text[:100] if reason_text else rpt_dst_txt,
        'reason_code':  rpt_dst_val,
        'stock_type':   '보통주',
        'shares_before': bfr,
        'shares_change': mdf,
        'shares_after':  ths,
        'price':         None,
        'hold_ratio_before': float(bfr_rt) if bfr_rt and bfr_rt != '-' else None,
        'hold_ratio_after':  float(ths_rt) if ths_rt and ths_rt != '-' else None,
        'base_date':    base_date.isoformat(),
        'report_type':  'bulk',  # 대량보유 구분
    }


def parse_trade_plan_xml(xml_content: str, rcept_no: str, corp_code: str,
                         corp_name: str, base_date: date) -> list[dict]:
    """
    임원·주요주주 거래계획보고서 XML 파싱
    핵심: 거래계획(TRAN_PLN) 섹션에서 예정 수량/방향/기간 추출
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    def find_val(acode):
        for te in root.iter('TE'):
            if te.get('ACODE') == acode:
                return (te.text or '').strip()
        return None

    def find_unit(aunit):
        for tu in root.iter('TU'):
            if tu.get('AUNIT') == aunit:
                return tu.get('AUNITVALUE', ''), (tu.text or '').strip()
        return '', ''

    # 보고자
    reporter = find_val('IFR_NM') or ''

    # 현재 보유 수량/비율
    cur_cnt = _parse_int(find_val('STK_CNT') or '')
    cur_rt  = find_val('STK_RT') or ''

    # 거래 완료 후 예상 보유 수량/비율
    own_cnt = _parse_int(find_val('OWN_STK_CNT') or '')
    own_rt  = find_val('OWN_STK_RT') or ''

    # 거래계획 섹션 (TABLE-GROUP ACLASS="TRAN_PLN")
    rows = []
    for tg in root.iter('TABLE-GROUP'):
        if tg.get('ACLASS') == 'TRAN_PLN':
            for tr in tg.iter('TR'):
                row_data = {}
                for child in tr:
                    aunit = child.get('AUNIT', '')
                    acode = child.get('ACODE', '')
                    val   = (child.text or '').strip()
                    uval  = child.get('AUNITVALUE', '')

                    if aunit == 'MDF_MT':
                        row_data['method_code'] = uval
                        row_data['method_txt']  = val
                    elif aunit == 'MDF_STR_DT':
                        row_data['plan_start'] = uval  # YYYYMMDD
                    elif aunit == 'MDF_END_DT':
                        row_data['plan_end']   = uval
                    elif aunit == 'STR_KND':
                        row_data['stock_type'] = val
                    elif acode == 'STR_STK_CNT':
                        row_data['plan_qty']   = _parse_int(val)
                    elif acode == 'ACI_AMT2':
                        row_data['plan_price'] = _parse_int(val)

                if 'method_code' not in row_data or not row_data.get('plan_qty'):
                    continue

                # 방향 판단: 처분(-) 계열 vs 취득(+) 계열
                mc = row_data['method_code']
                # 처분: 02(장내처분), 82(시간외매도), 12(장외처분) 등 짝수 계열
                # 취득: 01(장내취득), 81(시간외매수), 11(장외취득) 등
                if mc in ('02','12','22','32','42','52','62','72','82','92'):
                    direction = -1
                elif mc in ('01','11','21','31','41','51','61','71','81','91'):
                    direction = +1
                else:
                    direction = 0

                plan_qty = row_data.get('plan_qty', 0)
                shares_change = plan_qty * direction if direction != 0 else plan_qty

                # 기간 포맷
                ps = row_data.get('plan_start', '')
                pe = row_data.get('plan_end', '')
                period = ''
                if ps and pe and ps != '-' and pe != '-':
                    period = f"{ps[:4]}-{ps[4:6]}-{ps[6:8]}~{pe[:4]}-{pe[4:6]}-{pe[6:8]}"

                rows.append({
                    'rcept_no':      rcept_no,
                    'corp_code':     corp_code,
                    'corp_name':     corp_name,
                    'stock_code':    find_val('CRP_CD') or '',
                    'reporter':      reporter,
                    'relation':      find_val('MAIN_SH') or '',
                    'trade_date':    base_date.isoformat(),
                    'reason':        row_data.get('method_txt', '') or '',
                    'reason_code':   mc,
                    'stock_type':    row_data.get('stock_type', '보통주'),
                    'shares_before': cur_cnt,
                    'shares_change': shares_change,
                    'shares_after':  own_cnt,
                    'price':         row_data.get('plan_price'),
                    'hold_ratio_before': float(cur_rt) if cur_rt and cur_rt not in ('-','') else None,
                    'hold_ratio_after':  float(own_rt) if own_rt and own_rt not in ('-','') else None,
                    'base_date':     base_date.isoformat(),
                    'report_type':   'plan',
                    'plan_period':   period,
                })

    return rows


def fetch_and_parse(rcept_no: str, corp_code: str, corp_name: str,
                    base_date: date, api_key: str) -> list[dict]:
    """DART API 호출 → XML 파싱 → rows 반환"""
    url = 'https://opendart.fss.or.kr/api/document.xml'
    try:
        r = requests.get(url, params={'crtfc_key': api_key, 'rcept_no': rcept_no},
                         timeout=15)
        if r.status_code != 200:
            log.warning(f"[지분] {corp_name} HTTP {r.status_code}")
            return []

        z = zipfile.ZipFile(io.BytesIO(r.content))
        xml_files = [f for f in z.namelist() if f.endswith('.xml')]
        if not xml_files:
            return []

        content_bytes = z.read(xml_files[0])
        content = _decode_xml(content_bytes)

        return parse_insider_xml(content, rcept_no, corp_code, corp_name, base_date)

    except zipfile.BadZipFile:
        log.debug(f"[지분] {corp_name} ZIP 아님 (HTML 응답) — 스킵")
        return []
    except ET.ParseError as e:
        log.warning(f"[지분] {corp_name} XML 파싱 오류: {e}")
        return []
    except Exception as e:
        log.warning(f"[지분] {corp_name} 파싱 오류: {e}")
        return []


def collect_insider_trades(disc_all: list, api_key: str, sb,
                           base_date: date = None) -> int:
    """
    전체 공시 목록에서 지분공시 + 대량보유 파싱 → insider_trades upsert
    """
    if base_date is None:
        base_date = date.today()

    # ── 1. 임원·주요주주 소유상황보고서 (지분공시) ──
    SHARE_KEYWORDS   = ['임원ㆍ주요주주', '임원·주요주주', '주요주주특정증권']
    EXCLUDE_KEYWORDS = ['거래계획']
    insider_targets = [
        d for d in disc_all
        if any(kw in d.get('report_nm', '') for kw in SHARE_KEYWORDS)
        and not any(ex in d.get('report_nm', '') for ex in EXCLUDE_KEYWORDS)
        and d.get('rcept_no')
    ]

    # ── 2. 대량보유상황보고서 ──
    BULK_KEYWORDS = ['대량보유상황보고서']
    bulk_targets = [
        d for d in disc_all
        if any(kw in d.get('report_nm', '') for kw in BULK_KEYWORDS)
        and d.get('rcept_no')
    ]

    # ── 3. 최대주주등소유주식변동신고서 (HTML 형식, XML 파싱 불가) ──
    MAJOR_KEYWORDS = ['최대주주등소유주식변동', '최대주주변동']
    major_targets = [
        d for d in disc_all
        if any(kw in d.get('report_nm', '') for kw in MAJOR_KEYWORDS)
        and d.get('rcept_no')
    ]

    # ── 4. 거래계획보고서 ──
    PLAN_KEYWORDS = ['거래계획보고서']
    plan_targets = [
        d for d in disc_all
        if any(kw in d.get('report_nm', '') for kw in PLAN_KEYWORDS)
        and d.get('rcept_no')
    ]

    log.info(f"📋 [지분공시] 임원 {len(insider_targets)}건 + 대량보유 {len(bulk_targets)}건 + 최대주주변동 {len(major_targets)}건 + 거래계획 {len(plan_targets)}건")

    all_rows = []

    # ── 3. 최대주주변동 저장 (HTML 형식, XML 파싱 불가) ──
    for d in major_targets:
        all_rows.append({
            'rcept_no':      d['rcept_no'],
            'corp_code':     d.get('corp_code', ''),
            'corp_name':     d.get('corp_name', ''),
            'stock_code':    '',
            'reporter':      '',
            'relation':      '최대주주',
            'trade_date':    base_date.isoformat(),
            'reason':        d.get('report_nm', ''),
            'reason_code':   'major',
            'stock_type':    '보통주',
            'shares_before': None,
            'shares_change': None,
            'shares_after':  None,
            'price':         None,
            'hold_ratio_before': None,
            'hold_ratio_after':  None,
            'base_date':     base_date.isoformat(),
            'report_type':   'major',
        })

    # ── 임원 지분공시 파싱 ──
    for i, d in enumerate(insider_targets, 1):
        rows = fetch_and_parse(d['rcept_no'], d.get('corp_code',''), d.get('corp_name',''), base_date, api_key)
        for r in rows:
            r['report_type'] = 'insider'
        all_rows.extend(rows)
        time.sleep(0.3)

    # ── 거래계획 파싱 ──
    for d in plan_targets:
        rcept_no  = d['rcept_no']
        corp_code = d.get('corp_code', '')
        corp_name = d.get('corp_name', '')
        try:
            url = 'https://opendart.fss.or.kr/api/document.xml'
            r = requests.get(url, params={'crtfc_key': api_key, 'rcept_no': rcept_no}, timeout=15)
            z = zipfile.ZipFile(io.BytesIO(r.content))
            xml_files = [f for f in z.namelist() if f.endswith('.xml')]
            if not xml_files:
                continue
            content = _decode_xml(z.read(xml_files[0]))
            rows = parse_trade_plan_xml(content, rcept_no, corp_code, corp_name, base_date)
            all_rows.extend(rows)
        except zipfile.BadZipFile:
            log.debug(f"[거래계획] {corp_name} HTML 응답 — 스킵")
        except Exception as e:
            log.warning(f"[거래계획] {corp_name} 파싱 오류: {e}")
        time.sleep(0.3)

    # ── 대량보유 파싱 ──
    for i, d in enumerate(bulk_targets, 1):
        rcept_no  = d['rcept_no']
        corp_code = d.get('corp_code', '')
        corp_name = d.get('corp_name', '')
        try:
            url = 'https://opendart.fss.or.kr/api/document.xml'
            r = requests.get(url, params={'crtfc_key': api_key, 'rcept_no': rcept_no}, timeout=15)
            z = zipfile.ZipFile(io.BytesIO(r.content))
            xml_files = [f for f in z.namelist() if f.endswith('.xml')]
            if not xml_files:
                continue
            content = _decode_xml(z.read(xml_files[0]))
            row = parse_bulk_holder_xml(content, rcept_no, corp_code, corp_name, base_date)
            if row:
                all_rows.append(row)
        except zipfile.BadZipFile:
            log.debug(f"[대량보유] {corp_name} HTML 응답 — 스킵")
        except Exception as e:
            log.warning(f"[대량보유] {corp_name} 파싱 오류: {e}")
        time.sleep(0.3)

    if not all_rows:
        log.info("📋 [지분공시] 파싱 결과 없음")
        return 0

    # 배치 내 중복 제거
    seen_keys = {}
    deduped = []
    for row in all_rows:
        key = (row['rcept_no'], row.get('reporter',''), row.get('trade_date',''), row.get('stock_type',''))
        if key not in seen_keys:
            seen_keys[key] = True
            deduped.append(row)
    all_rows = deduped

    # upsert (10개씩 배치) — collect_utils.batch_upsert 위임 (청크별 try/except·건수반환 동일)
    saved = batch_upsert(sb, 'insider_trades', all_rows,
                         'rcept_no,reporter,trade_date,stock_type', chunk=10)

    log.info(f"📋 [지분공시] 저장 완료: {saved}건 (임원 {len(insider_targets)}건 + 대량보유 {len(bulk_targets)}건)")
    return saved
