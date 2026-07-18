"""
jobs_collect.py — 데이터 수집·집계 잡
─────────────────────────────────────
run_all.py 물리 분할 (2026-07): 재무·시장·수급·신고가·공매도·매크로·요약 생성 등
스케줄 수집 잡 본문. 공통 인프라는 job_infra.py.
"""
import os
import re
import json
import time
import logging
import datetime

import stock_api
from managers import market_timer
from db_utils import fetch_all_pages
from format_utils import fmt_change_pct
from telegram_utils import get_admin_chat_id as _get_admin_chat_id
from config import DEFAULT_CHAT_ID, CHAT_IDS_BY_CODE
from job_infra import _job, _is_enabled, _log_notice, _bridge, _BRIDGE_OK

# ✅ 재무/시장 데이터 수집 + 상장사 동기화
try:
    from collect_financials        import run as run_financials, auto_detect_quarter
    # run_financials(year, quarter, monitored_only=True) 형태로 호출 가능
    from collect_market            import run as run_market
    from collect_listed_companies  import run as run_sync_companies
    from grade                     import save_grade_history as _save_grade_history, save_trend_flags as _save_trend_flags
    from collect_insider           import collect_insider_trades as _collect_insider
    from collect_company_info      import collect_one as _collect_company_one
    from collect_short             import run as run_short, check_surge as _check_short_surge, format_surge_msg as _format_surge_msg
    _COLLECTOR_OK = True
except ImportError as _ie:
    _COLLECTOR_OK = False
    run_financials = auto_detect_quarter = run_market = run_sync_companies = None
    _save_grade_history = _save_trend_flags = _collect_insider = None
    _collect_company_one = run_short = _check_short_surge = _format_surge_msg = None
    logging.warning(f"수집 모듈 없음 (무시): {_ie}")


@_job()
def job_sync_listed_companies():
    """토요일 새벽 1시 — 코스피+코스닥 전체 상장사 동기화 (신규/사명변경/상폐)"""
    if not _COLLECTOR_OK:
        logging.warning("collect_listed_companies.py 없음 — 스킵")
        return
    try:
        logging.info("🏢 [상장사동기화] 시작")
        run_sync_companies()
        logging.info("🏢 [상장사동기화] 완료")
        _log_notice("system", "[상장사동기화] 완료")
    except Exception as e:
        logging.error(f"❌ [상장사동기화] 오류: {e}")


@_job()
def job_cleanup_market_data():
    """토요일 새벽 — market_data 정리
    - 모니터링 종목: 90일 보존
    - 전체 상장사(비모니터링): 28일 보존
    """
    KEEP_MON  = 90   # 모니터링 종목 보존일
    KEEP_ALL  = 28   # 전체 종목 보존일
    try:
        sb = _bridge._get_client() if _BRIDGE_OK else None
        if not sb:
            logging.error("❌ [정리] Supabase 연결 없음 — 스킵")
            return

        cutoff_all = (datetime.date.today() - datetime.timedelta(days=KEEP_ALL)).isoformat()
        cutoff_mon = (datetime.date.today() - datetime.timedelta(days=KEEP_MON)).isoformat()

        # 모니터링 종목 코드 목록
        _mon = sb.table('companies').select('code').eq('is_monitored', True).execute()
        mon_codes = [r['code'].split('.')[0] for r in (_mon.data or [])]

        # 1) 비모니터링 종목 — 28일 초과 삭제
        if mon_codes:
            sb.table('market_data').delete() \
              .lt('base_date', cutoff_all) \
              .not_.in_('stock_code', mon_codes).execute()
        else:
            sb.table('market_data').delete().lt('base_date', cutoff_all).execute()

        # 2) 모니터링 종목 — 90일 초과 삭제
        if mon_codes:
            # 500개 청크로 분할
            chunk = 200
            for i in range(0, len(mon_codes), chunk):
                sb.table('market_data').delete() \
                  .lt('base_date', cutoff_mon) \
                  .in_('stock_code', mon_codes[i:i+chunk]).execute()

        logging.info(f"🗑️ [정리] market_data 정리 완료 — 모니터링 {KEEP_MON}일 / 전체 {KEEP_ALL}일 보존")
    except Exception as e:
        logging.error(f"❌ [정리] market_data 정리 오류: {e}")


def _preprocess_disclosures(all_disc_records: list, sb) -> list:
    """
    app_config 저장 전 전처리:
      1. 노이즈 공시 제거 (NOISE_KEYWORDS)
      2. 비상장 제거 (stock_code 없는 것) — companies 테이블 조회
      3. 시총 1000억 미만 제거 — market_data 조회 (market_cap 필드도 첨부)
      4. 카테고리 분류 — category 필드 추가
      5. insider 요약 — insider_summary 필드 추가 (rcept_no 기준)
    반환: 전처리된 레코드 리스트 (각 항목에 category, market_cap, insider_summary 포함)
    """

    # ── 1. 노이즈 제거 ──────────────────────────────────────────────
    NOISE_KEYWORDS = [
        '효력발생안내', '투자설명서(일괄신고)', '일괄신고추가서류',
        '일괄신고서(', '증권발행실적보고서', '[발행조건확정]',
        '해산사유발생', '의결권대리행사권유',
        '동일인등출자계열회사와의상품ㆍ용역거래',
        '정정신고서제출요구', '소속부변경', '소액공모공시서류', '집합투자증권',
    ]
    records = [
        d for d in all_disc_records
        if not any(kw in (d.get('report_nm') or '') for kw in NOISE_KEYWORDS)
    ]
    logging.info(f"📋 [전처리] 노이즈 제거: {len(all_disc_records)}→{len(records)}건")

    # ── 2 & 3. companies + market_data 조회 (상장 여부 + 시총) ─────
    corp_codes = list({d['corp_code'] for d in records if d.get('corp_code')})
    listed_set = set()    # 상장 corp_code
    cap_map    = {}       # corp_code → market_cap

    try:
        # 2+3. 상장 여부 + corp_code→stock_code 매핑 — 동일 쿼리 2회 반복하던 것을 1회로 통합
        chunk = 500
        stock_map = {}   # corp_code → stock_code
        for i in range(0, len(corp_codes), chunk):
            res = sb.table('companies').select('corp_code,code') \
                    .in_('corp_code', corp_codes[i:i+chunk]).execute()
            for c in (res.data or []):
                listed_set.add(c['corp_code'])
                sc = (c.get('code') or '').replace('.KS', '').replace('.KQ', '')
                if sc:
                    stock_map[c['corp_code']] = sc

        stock_codes = list(set(stock_map.values()))
        if stock_codes:
            date_res = sb.table('market_data').select('base_date') \
                         .order('base_date', desc=True).limit(1).execute()
            max_date = (date_res.data or [{}])[0].get('base_date')
            if max_date:
                mkt_map = {}
                for i in range(0, len(stock_codes), chunk):
                    mkt_res = sb.table('market_data').select('stock_code,market_cap') \
                                .eq('base_date', max_date) \
                                .in_('stock_code', stock_codes[i:i+chunk]).execute()
                    mkt_map.update({m['stock_code']: m['market_cap'] for m in (mkt_res.data or [])})
                for cc, sc in stock_map.items():
                    if sc in mkt_map:
                        cap_map[cc] = mkt_map[sc]

        logging.info(f"📋 [전처리] 상장사 {len(listed_set)}개 / 시총 {len(cap_map)}개 조회 완료")
    except Exception as e:
        logging.warning(f"⚠️ [전처리] companies/market_data 조회 실패 (필터 스킵): {e}")

    CAP_THRESHOLD = 100_000_000_000  # 1000억

    filtered = []
    for d in records:
        cc = d.get('corp_code')
        # 비상장 제거 (listed_set에 없으면 제외 — 단, listed_set이 비어있으면 전부 통과)
        if listed_set and cc and cc not in listed_set:
            continue
        # 시총 1000억 미만 제거 (market_cap 정보가 있는 경우만)
        cap = cap_map.get(cc)
        if cap is not None and cap < CAP_THRESHOLD:
            continue
        filtered.append(d)
    logging.info(f"📋 [전처리] 상장/시총 필터: {len(records)}→{len(filtered)}건")

    # ── 4. 카테고리 분류 ───────────────────────────────────────────
    CATEGORIES = [
        ('사업보고서',    ['사업보고서']),
        ('반기보고서',    ['반기보고서']),
        ('분기보고서',    ['분기보고서']),
        ('기업설명회(IR)',['기업설명회', 'IR개최', 'NDR']),
        ('잠정실적',      ['잠정', '결산실적', '실적(공정공시)']),
        ('주요사항',      ['주요사항보고']),
        ('증자/감자',     ['유상증자', '무상증자', '감자']),
        ('합병/분할',     ['합병', '분할', '영업양수', '영업양도']),
        ('사채/전환',     ['전환사채', '신주인수권', '교환사채', '사채권']),
        ('자사주',        ['자기주식']),
        ('배당',          ['배당']),
        ('최대주주변동',  ['최대주주등소유주식변동', '최대주주변동']),
        ('대량보유',      ['대량보유상황보고서', '대량보유']),
        ('거래계획(예고)',['거래계획보고서']),
        ('거래계획(철회)',['거래계획철회보고서']),
        ('지분공시',      ['소유상황보고서', '임원ㆍ주요주주', '임원·주요주주']),
        ('임원/주식',     ['임원', '주요주주']),
        ('감사보고서',    ['감사보고서', '내부회계']),
        ('공정공시',      ['공정공시']),
        ('주식매수선택권',['주식매수선택권']),
        ('주요경영사항',  [
            '투자판단관련주요경영사항', '단일판매ㆍ공급계약', '단일판매·공급계약',
            '소송등의판결', '타인에대한채무보증', '특수관계인으로부터자금차입',
            '특수관계인에대한자금대여', '특수관계인에대한출자', '특수관계인으로부터자산양수',
            '특수관계인의유상증자참여', '금전대여결정', '단기차입금증가',
            '기업가치제고계획', '대표이사변경', '사외이사의선임',
            '증권발행결과', '주식병합결정', '감자완료',
        ]),
        ('증권신고',      ['증권신고서', '투자설명서']),
    ]

    def _classify(report_nm: str) -> str:
        for label, keywords in CATEGORIES:
            if any(kw in report_nm for kw in keywords):
                return label
        return '기타'

    for d in filtered:
        d['category'] = _classify(d.get('report_nm') or '')

    # ── 5. insider 요약 첨부 (insider_trades DB에서 오늘 데이터 조회) ──
    try:
        today_str = datetime.date.today().isoformat()
        ins_res = sb.table('insider_trades') \
                    .select('rcept_no,corp_code,reporter,shares_change,hold_ratio_before,hold_ratio_after,plan_period,report_type') \
                    .eq('base_date', today_str).execute()
        ins_rows = ins_res.data or []

        ins_map      = {}  # rcept_no → [rows]
        corp_ins_map = {}  # corp_code → [rows]
        for r in ins_rows:
            if r.get('rcept_no'):
                ins_map.setdefault(r['rcept_no'], []).append(r)
            if r.get('corp_code'):
                corp_ins_map.setdefault(r['corp_code'], []).append(r)

        for d in filtered:
            rno = d.get('rcept_no')
            cc  = d.get('corp_code')
            cat = d.get('category', '')
            summary = None

            if cat == '지분공시' and cc and cc in corp_ins_map:
                rows = corp_ins_map[cc]
                buy  = sum(r['shares_change'] for r in rows if (r.get('shares_change') or 0) > 0)
                sell = sum(abs(r['shares_change']) for r in rows if (r.get('shares_change') or 0) < 0)
                summary = {'type': 'insider', 'buy': buy, 'sell': sell, 'count': len(rows)}
            elif cat == '대량보유' and rno and rno in ins_map:
                row = ins_map[rno][0]
                chg = row.get('shares_change') or 0
                summary = {
                    'type': 'bulk', 'change': chg,
                    'ratio_before': row.get('hold_ratio_before'),
                    'ratio_after':  row.get('hold_ratio_after'),
                }
            elif cat == '최대주주변동' and rno and rno in ins_map:
                summary = {'type': 'major'}
            elif cat == '거래계획(예고)' and rno and rno in ins_map:
                rows = ins_map[rno]
                buy  = sum(r['shares_change'] for r in rows if (r.get('shares_change') or 0) > 0)
                sell = sum(abs(r['shares_change']) for r in rows if (r.get('shares_change') or 0) < 0)
                periods = list({r['plan_period'] for r in rows if r.get('plan_period')})
                summary = {'type': 'plan', 'buy': buy, 'sell': sell,
                           'period': periods[0] if periods else None}
            if summary:
                d['insider_summary'] = summary

        logging.info(f"📋 [전처리] insider 요약 첨부 완료 ({len(ins_rows)}건 참조)")
    except Exception as e:
        logging.warning(f"⚠️ [전처리] insider 요약 첨부 실패 (스킵): {e}")

    return filtered


@_job()
def job_collect_financials():
    """평일 18:30 — 오늘 DART 공시된 종목만 재무 수집"""
    if not _COLLECTOR_OK:
        logging.warning("collect_financials.py 없음 — 수집 스킵")
        return
    if not _is_enabled('collect_financials'):
        logging.info("⏸ 재무수집 비활성화 (DB 설정)")
        return
    try:
        import OpenDartReader
        dart   = OpenDartReader(os.environ.get('DART_API_KEY',''))
        today  = datetime.date.today().strftime('%Y%m%d')
        today_display = datetime.date.today().strftime('%Y-%m-%d')  # 가독성용
        year, quarter = auto_detect_quarter()

        # ── 오늘 전체 공시 조회 (final=False → 임원변동, 주요사항 등 모두 포함) ──
        disc_all  = dart.list(start=today, end=today, final=False)

        # ── 재무수집용: 정기보고서 최종본만 (final=True → 정정 전 원본 중복 방지) ──
        disc_fin  = dart.list(start=today, end=today, final=True)

        # 전체 공시 목록 구성 (disc_all 기준)
        all_disc_records = []
        seen_rcept = set()
        if disc_all is not None and not disc_all.empty:
            for _, row in disc_all.iterrows():
                rno = row.get('rcept_no', '')
                seen_rcept.add(rno)
                all_disc_records.append({
                    'corp_name': row.get('corp_name', ''),
                    'report_nm': row.get('report_nm', ''),
                    'rcept_dt':  row.get('rcept_dt',  ''),
                    'corp_code': row.get('corp_code', ''),
                    'rcept_no':  row.get('rcept_no',  ''),
                })

        # disc_fin의 정기보고서 중 disc_all에 없는 것 병합
        # (final=False에서 누락되는 경우 보완)
        target_types = ['분기보고서', '반기보고서', '사업보고서']
        if disc_fin is not None and not disc_fin.empty:
            fin_only = disc_fin[disc_fin['report_nm'].str.contains('|'.join(target_types), na=False)]
            for _, row in fin_only.iterrows():
                rno = row.get('rcept_no', '')
                if rno and rno not in seen_rcept:
                    all_disc_records.append({
                        'corp_name': row.get('corp_name', ''),
                        'report_nm': row.get('report_nm', ''),
                        'rcept_dt':  row.get('rcept_dt',  ''),
                        'corp_code': row.get('corp_code', ''),
                        'rcept_no':  rno,
                    })
                    seen_rcept.add(rno)

        # ── 분기/반기/사업보고서만 필터 (재무수집 + 실적공시 카드용) ──
        def _parse_quarter(report_nm: str):
            """report_nm에서 실제 분기 파싱. 예: '사업보고서 (2025.12)' → ('2025', 'Q4')"""
            m = re.search(r'\((\d{4})\.(\d{2})\)', report_nm)
            if not m:
                return None, None
            y, mo = m.group(1), int(m.group(2))
            return y, {3:'Q1', 6:'Q2', 9:'Q3', 12:'Q4'}.get(mo)

        today_corps    = []
        quarter_groups = {}  # {(year, quarter): [corp_code]} — 정기보고서 없는 날 NameError 방지
        if disc_fin is not None and not disc_fin.empty:
            filtered = disc_fin[
                disc_fin['report_nm'].str.contains('|'.join(target_types), na=False) &
                ~disc_fin['report_nm'].str.contains('정정|기재정정', na=False)  # ✅ 정정 공시 제외
            ]
            if not filtered.empty:
                # 종목별로 report_nm에서 실제 분기 파싱 → 분기별 그룹핑
                from collect_financials import run_by_corp_codes
                quarter_groups = {}  # {(year, quarter): [corp_code, ...]}
                seen = {}

                for _, row in filtered.iterrows():
                    code  = row['corp_code']
                    nm    = row.get('report_nm', '')
                    is_amended = '정정' in nm or '기재정정' in nm
                    y, q  = _parse_quarter(nm)

                    # 동일 corp_code 중복 제거 (정정 우선)
                    if code not in seen or (is_amended and not seen[code]['is_amended']):
                        seen[code] = {
                            'corp_code':  code,
                            'corp_name':  row.get('corp_name', ''),
                            'report_nm':  nm,
                            'is_amended': is_amended,
                        }
                        if y and q:
                            key = (y, q)
                            quarter_groups.setdefault(key, [])
                            if code not in quarter_groups[key]:
                                quarter_groups[key].append(code)

                today_corps = list(seen.values())

                # ── KOSPI/KOSDAQ 상장사만 필터 ──────────────────────────
                # DART corp_cls: Y=유가증권(KOSPI), K=코스닥, E=코넥스, N=기타(외감법인/펀드 등)
                # disc_fin에 corp_cls 컬럼이 있으면 직접 필터
                if 'corp_cls' in filtered.columns:
                    listed_codes = set(
                        filtered[filtered['corp_cls'].isin(['Y', 'K'])]['corp_code'].tolist()
                    )
                    today_corps = [c for c in today_corps if c['corp_code'] in listed_codes]
                    logging.info(f"📊 [재무수집] 상장사 필터 적용: {len(seen)}개 → {len(today_corps)}개")
                else:
                    # corp_cls 없으면 companies DB에서 확인
                    if _BRIDGE_OK:
                        try:
                            _sb_tmp = _bridge._get_client()
                            all_corp_codes = [c['corp_code'] for c in today_corps if c.get('corp_code')]
                            if all_corp_codes:
                                chunk = 500
                                listed_set = set()
                                for i in range(0, len(all_corp_codes), chunk):
                                    _res = _sb_tmp.table('companies').select('corp_code') \
                                                  .in_('corp_code', all_corp_codes[i:i+chunk]).execute()
                                    for r in (_res.data or []):
                                        listed_set.add(r['corp_code'])
                                today_corps = [c for c in today_corps if c['corp_code'] in listed_set]
                                logging.info(f"📊 [재무수집] 상장사 필터(DB): {len(seen)}개 → {len(today_corps)}개")
                        except Exception as _fe:
                            logging.warning(f"⚠️ [재무수집] 상장사 필터 실패 (전체 포함): {_fe}")

                # 분기별로 나눠서 수집
                total_ok, total_fail = 0, 0
                for (y, q), codes in quarter_groups.items():
                    logging.info(f"📊 [재무수집] {y} {q} — {len(codes)}개 종목 수집 시작")
                    ok, fail = run_by_corp_codes(codes, y, q)
                    total_ok += ok; total_fail += fail
                    logging.info(f"📊 [재무수집] {y} {q} 완료: 성공 {ok}개, 실패 {fail}개")

                logging.info(f"📊 [재무수집] 전체 완료: 성공 {total_ok}개, 실패 {total_fail}개")
                _log_notice("system", f"[재무수집] 오늘 공시 {len(today_corps)}개 → {total_ok}개 완료")
            else:
                logging.info(f"📊 [재무수집] 오늘 재무 공시 없음 — 재무수집 스킵, 공시목록만 저장")
        else:
            logging.info(f"📊 [재무수집] 오늘({today}) 정기보고서 공시 없음 — 공시목록만 저장")

        # ── 지분공시 파싱 (임원 매수/매도) — app_config 저장 전에 실행 ──
        if all_disc_records and _COLLECTOR_OK and _BRIDGE_OK:
            try:
                dart_key = os.environ.get('DART_API_KEY', '')
                sb2 = _bridge._get_client()
                _collect_insider(all_disc_records, dart_key, sb2)
            except Exception as _ie:
                logging.error(f"❌ [지분공시] 수집 오류: {_ie}")

        # ── 공시 저장 (항상 실행 — 날짜 기준으로 갱신) ──────────────
        if _BRIDGE_OK:
            sb = _bridge._get_client()

            # 전체 공시: 저장 전 전처리 (노이즈 제거 + 상장 필터 + 시총 + 카테고리 + insider 요약)
            # insider_trades는 이미 위에서 _collect_insider로 저장 완료된 상태이므로
            # _preprocess_disclosures가 insider_trades DB를 바로 읽어 요약 첨부 가능
            processed_disclosures = _preprocess_disclosures(all_disc_records, sb)

            # ── 1. 실적 공시 종목 → app_config (소량 JSON, 유지) ──
            sb.table('app_config').upsert({
                'key':         'today_earnings_corps',
                'value':       json.dumps(today_corps, ensure_ascii=False),
                'description': f'{today_display} 실적 공시 종목 목록'
            }, on_conflict='key').execute()

            # ── 2. 전체 공시 → daily_disclosures 테이블 (app_config JSON 탈출) ──
            _today_str = datetime.date.today().isoformat()

            # 행 구성을 먼저 끝낸 뒤 삭제→삽입 — 프론트에 빈 데이터가 노출되는 창 최소화
            rows_to_insert = [{
                'base_date':       _today_str,
                'corp_code':       d.get('corp_code') or '',
                'corp_name':       d.get('corp_name') or '',
                'report_nm':       d.get('report_nm') or '',
                'rcept_no':        d.get('rcept_no') or None,
                'rcept_dt':        d.get('rcept_dt') or None,
                'category':        d.get('category') or '기타',
                'market_cap':      d.get('market_cap') or None,
                'insider_summary': d.get('insider_summary') or None,
            } for d in processed_disclosures]

            # 공시 0건이면 삭제 스킵 — DART 빈 응답(장애/수동 트리거) 시
            # delete→insert가 그날 저장분을 통째로 지우는 것 방지
            if not rows_to_insert:
                logging.warning("⚠️ [공시저장] 오늘 공시 0건 — 기존 daily_disclosures 보존(삭제 스킵)")
            else:
                sb.table('daily_disclosures').delete().eq('base_date', _today_str).execute()

                # 청크별 2회 재시도 — 부분 저장(중간 실패로 일부 공시 유실) 감지·복구
                _failed_chunks = 0
                for i in range(0, len(rows_to_insert), 100):
                    _chunk_rows = rows_to_insert[i:i + 100]
                    for _attempt in (1, 2):
                        try:
                            sb.table('daily_disclosures').insert(_chunk_rows).execute()
                            break
                        except Exception as _ins_e:
                            if _attempt == 2:
                                _failed_chunks += 1
                                logging.error(f"❌ [공시저장] 청크 {i // 100} 삽입 실패(2회): {_ins_e}")
                            else:
                                time.sleep(2)
                if _failed_chunks:
                    logging.error(f"❌ [공시저장] {_failed_chunks}개 청크 유실 — daily_disclosures 부분 저장 상태")

            logging.info(
                f"📊 [공시저장] 실적공시 {len(today_corps)}건 / "
                f"전체공시 {len(all_disc_records)}→{len(processed_disclosures)}건 "
                f"daily_disclosures 저장 완료"
            )

        # 재무수집 완료 후 등급 이력 + 추세 신호 저장
        if today_corps and quarter_groups:
            for (y, q) in quarter_groups.keys():
                job_save_grade_history(y, q)
                job_save_trend_flags(y, q)

    except Exception as e:
        logging.error(f"❌ [재무수집] 오류: {e}")


def job_save_grade_history(year: str = None, quarter: str = None):
    """재무수집 완료 후 — 등급 계산 & earnings_grade_history 저장 + 등급 변경 알림"""
    if not _BRIDGE_OK:
        return
    try:
        if not year or not quarter:
            year, quarter = auto_detect_quarter()
        sb = _bridge._get_client()
        result = _save_grade_history(sb, year, quarter)

        # ── 등급 변경 알림 발송 ──────────────────────────────────────
        new_list  = result.get('new',  [])
        up_list   = result.get('up',   [])
        down_list = result.get('down', [])

        if not (new_list or up_list or down_list):
            return

        GRADE_EMOJI = {'S': '🏆', 'A': '🥇', 'B': '🥈', '관찰': '⚡'}
        GRADE_COLOR = {'S': '금', 'A': '주황', 'B': '파랑', '관찰': '초록'}

        def fmt_row(r, change_label):
            g  = r['grade']
            em = GRADE_EMOJI.get(g, '')
            rev_str = ''
            if r.get('rev_yoy') is not None:
                rev_str = f" | YoY {fmt_change_pct(r['rev_yoy'], 1)}"
            op_str = ''
            if r.get('op_yoy') is not None:
                op_str = f" / 영업익 {fmt_change_pct(r['op_yoy'], 1)}"
            return f"{em} <b>{r['corp_name']}</b> {g}급 {change_label}{rev_str}{op_str}"

        sections = []

        if new_list:
            lines = [fmt_row(r, '신규진입') for r in new_list[:15]]
            if len(new_list) > 15:
                lines.append(f"... 외 {len(new_list) - 15}개")
            sections.append("🆕 <b>신규 진입</b>\n" + "\n".join(lines))

        if up_list:
            lines = [fmt_row(r, '↑') for r in up_list[:10]]
            if len(up_list) > 10:
                lines.append(f"... 외 {len(up_list) - 10}개")
            sections.append("📈 <b>등급 향상</b>\n" + "\n".join(lines))

        if down_list:
            lines = [fmt_row(r, '↓') for r in down_list[:10]]
            if len(down_list) > 10:
                lines.append(f"... 외 {len(down_list) - 10}개")
            sections.append("📉 <b>등급 하락</b>\n" + "\n".join(lines))

        msg = (
            f"📊 <b>[실적 등급 변동] {year} {quarter}</b>\n"
            f"════════════\n"
            + "\n\n".join(sections)
        )

        # 개인 채팅방(admin_chat_id)으로만 발송
        target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)
        stock_api.send_telegram(target, msg)
        logging.info(
            f"📢 [등급알림] {year} {quarter} → {target} — "
            f"신규 {len(new_list)}개 / 향상 {len(up_list)}개 / 하락 {len(down_list)}개"
        )

    except Exception as e:
        logging.error(f"❌ [등급이력] 오류: {e}")


def job_save_trend_flags(year: str = None, quarter: str = None):
    """재무수집/등급이력 완료 후 — 재무 추세 신호 탐지 & financials.trend_flags 저장"""
    if not _BRIDGE_OK or not _COLLECTOR_OK:
        return
    try:
        if not year or not quarter:
            year, quarter = auto_detect_quarter()
        sb = _bridge._get_client()
        flagged = _save_trend_flags(sb, year, quarter)
        logging.info(f"📈 [추세신호] {year} {quarter} 완료 — 경고 {flagged}개")
    except Exception as e:
        logging.error(f"❌ [추세신호] 오류: {e}")


@_job()
def job_collect_macro():
    """글로벌 매크로 데이터 수집 (지수/환율/원자재) — 06:30 아침 수집 시 메인 채널 발송 포함"""
    if not _is_enabled('collect_macro'):
        return
    # 주말 스킵 — 단, 토요일 새벽(06시대)은 미국 금요일장 마감분 수집을 위해 허용.
    # 일요일·주말 저녁 실행은 토요일 값의 복제 행만 만들어 macro_data에
    # 비거래일 base_date를 쌓는다 (섹터요약 거래일 산정 오염의 원인이었음).
    _now = datetime.datetime.now()
    if _now.weekday() >= 5 and not (_now.weekday() == 5 and _now.hour == 6):
        logging.info("⏸ [매크로] 주말 스킵 (토 06시대 미국 마감분 수집만 허용)")
        return
    logging.info("=== 매크로 데이터 수집 시작 ===")
    try:
        import collect_macro
        data = collect_macro.collect_all()
        collect_macro.save_to_db(data)
        logging.info("=== 매크로 데이터 수집 완료 ===")

        # 06:xx 아침 수집에만 메인 채널 브리핑 발송 (16:10 저녁 수집 제외, 휴장일 제외)
        if datetime.datetime.now().hour == 6 and _is_enabled('macro_briefing') and not market_timer.is_us_holiday():
            msg = stock_api.get_macro_briefing(data)
            if msg:
                stock_api.send_telegram(DEFAULT_CHAT_ID, msg)
                _log_notice(DEFAULT_CHAT_ID, "[매크로 브리핑] 발송")
                logging.info("📊 [매크로 브리핑] 메인 채널 발송 완료")
    except Exception as e:
        logging.error(f"매크로 데이터 수집 실패: {e}")


@_job(weekday_only=True)
def job_collect_analyst_opinions():
    """증권사 투자의견 수집 (하루 2회: 장전 + 장후)"""
    logging.info("📋 [투자의견] 증권사 투자의견 수집 시작")
    try:
        import collect_market
        saved = collect_market.collect_analyst_opinions()
        logging.info(f"📋 [투자의견] 완료: {saved}건 저장")
    except Exception as e:
        logging.error(f"❌ [투자의견] 수집 실패: {e}")


@_job(weekday_only=True)
def job_collect_foreign_institution():
    """기관/외국인 매매가집계 수집 (장중 4회 + 장마감 후)"""
    logging.info("=== 기관/외국인 수급 수집 시작 ===")
    try:
        import collect_market
        result = collect_market.collect_foreign_institution()
        logging.info(f"=== 수급 수집 완료: 외국인 {len(result['frgn_buy'])}개 / 기관 {len(result['orgn_buy'])}개 / 동시매수 {len(result['both_buy'])}개 ===")
    except Exception as e:
        logging.error(f"❌ 수급 수집 실패: {e}")


@_job(weekday_only=True)
def job_collect_new_high():
    """장 마감 후 신고가 종목 수집 + 알림 발송"""
    logging.info("=== 신고가 종목 수집 시작 ===")
    try:
        import collect_market
        rows = collect_market.collect_new_high()
        logging.info(f"=== 신고가 수집 완료: {len(rows)}개 ===")
        _alert_new_high(rows)
    except Exception as e:
        logging.error(f"❌ 신고가 수집 실패: {e}")


def _alert_new_high(rows: list):
    """
    52주 신고가 갱신 알림 발송.
    ① 모니터링 종목 → 해당 종목 채팅방 개별 발송
    ② 모니터링 종목 중 신고가 갱신 전체 → 메인 채팅방 묶음 발송
    """
    if not rows:
        return

    # 등락률 내림차순 정렬 (상한가/강한 돌파가 위로)
    rows = sorted(rows, key=lambda x: (x.get('chg_pct') or 0), reverse=True)

    today = datetime.date.today().isoformat()

    # 모니터링 종목 코드 → 채팅방 ID 매핑 (CHAT_IDS_BY_CODE: {code: chat_id})
    monitored = {}  # code → chat_id
    for code, chat_id in CHAT_IDS_BY_CODE.items():
        monitored[str(code).strip()] = chat_id

    main_lines = []  # 메인방 묶음용

    for r in rows:
        code     = str(r.get('code', '')).strip()
        name     = r.get('name', code)
        price    = r.get('price', 0) or 0
        chg_pct  = r.get('chg_pct', 0) or 0
        d52_high = r.get('d52_high', 0) or 0
        d52_low  = r.get('d52_low',  0) or 0

        chg_str  = fmt_change_pct(chg_pct)
        chg_icon = '🔺' if chg_pct > 0 else ('🔻' if chg_pct < 0 else '➖')

        # ① 종목 채팅방 개별 알림
        if code in monitored:
            msg = (
                f"🏆 <b>[{name}] 52주 신고가 갱신!</b>\n"
                f"════════════\n"
                f"💰 현재가: {price:,}원 ({chg_icon}{chg_str})\n"
                f"📈 52주 고가: {d52_high:,}원\n"
                f"📉 52주 저가: {d52_low:,}원"
            )
            stock_api.send_telegram(monitored[code], msg)

        # ② 메인방 묶음용 라인 누적
        main_lines.append(f"• <b>{name}</b>  {price:,}원 ({chg_icon}{chg_str})")

    # ② 메인 채팅방 묶음 발송
    if main_lines:
        header = (
            f"🏆 <b>[52주 신고가 갱신] {today}</b>\n"
            f"════════════\n"
        )
        stock_api.send_telegram(DEFAULT_CHAT_ID, header + "\n".join(main_lines))
        logging.info(f"🏆 [신고가 알림] 메인방 {len(main_lines)}개 발송")


@_job("collect_us_etf")
def job_collect_us_etf():
    """미국 산업별 ETF 수집 → us_market 테이블"""
    logging.info("=== US ETF 수집 시작 ===")
    try:
        import collect_us_etf
        collect_us_etf.collect_and_save(days=5)
        logging.info("=== US ETF 수집 완료 ===")
    except Exception as e:
        logging.error(f"❌ US ETF 수집 실패: {e}")


@_job()
def job_collect_market():
    """평일 장중 — KIS 모니터링 종목(306개) 시장 데이터 수집"""
    if datetime.datetime.now().weekday() >= 5 or market_timer.is_kr_holiday():
        logging.info("⏸ [시장수집] 주말/공휴일 스킵")
        return
    if not _COLLECTOR_OK:
        return
    try:
        logging.info("📈 [시장수집] 시작")
        ok, fail = run_market()
        logging.info(f"📈 [시장수집] 완료: 성공 {ok}개, 실패 {fail}개")
        _log_notice("system", f"[시장수집] 완료 ({ok}개)")
    except Exception as e:
        logging.error(f"❌ [시장수집] 오류: {e}")


def _check_market_warnings():
    """
    모니터링 종목 중 관리종목·투자유의·시장경보 진입 종목 감지 → 개인 채팅방 알림.
    시장경보 코드: 00=정상, 01=주의, 02=경고, 03=위험
    """
    if not _BRIDGE_OK:
        return
    try:
        sb = _bridge._get_client()

        # 최근 2개 거래일 (주말/공휴일 무관 — market_data엔 거래일만 존재)
        dres = sb.table('market_data').select('base_date') \
                 .order('base_date', desc=True).limit(1).execute()
        latest = (dres.data or [{}])[0].get('base_date')
        if not latest:
            return
        pres = sb.table('market_data').select('base_date') \
                 .lt('base_date', latest).order('base_date', desc=True).limit(1).execute()
        prev = (pres.data or [{}])[0].get('base_date')

        # 최신 거래일 경보 종목 (전체시장 — PostgREST 1000행 한도 회피)
        alerts = fetch_all_pages(
            sb.table('market_data')
              .select('stock_code,corp_name,market_warn_code,is_caution,price,price_change_rate')
              .eq('base_date', latest)
              .or_('is_caution.eq.true,market_warn_code.neq.00')
        )
        if not alerts:
            return

        # 직전 거래일 경보 이력 (신규 진입만 알림)
        prev_warn = set()
        if prev:
            prev_rows = fetch_all_pages(
                sb.table('market_data')
                  .select('stock_code')
                  .eq('base_date', prev)
                  .or_('is_caution.eq.true,market_warn_code.neq.00')
            )
            prev_warn = {r['stock_code'] for r in prev_rows}

        # 신규 진입만 필터
        new_alerts = [a for a in alerts if a['stock_code'] not in prev_warn]
        if not new_alerts:
            return

        target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)

        def _chg_str(a):
            r = a.get('price_change_rate') or 0
            return fmt_change_pct(r)

        # 그룹별 분류
        GROUPS = [
            ('03', '🆘 투자위험 종목 지정',   lambda a: a.get('market_warn_code') == '03'),
            ('02', '🚨 투자경고 종목 지정',   lambda a: a.get('market_warn_code') == '02'),
            ('01', '⚠️ 투자주의 종목 지정',   lambda a: a.get('market_warn_code') == '01'),
            ('ca', '🔵 투자유의 종목 지정',   lambda a: a.get('is_caution') and (a.get('market_warn_code') or '00') == '00'),
        ]

        sections = []
        for _, label, fn in GROUPS:
            group = [a for a in new_alerts if fn(a)]
            if not group:
                continue
            lines = [
                f"• <b>{a['corp_name']}</b> ({a.get('price', 0):,}원 {_chg_str(a)})"
                for a in group
            ]
            sections.append(f"<b>{label} ({len(group)})</b>\n" + "\n".join(lines))

        if not sections:
            return

        msg = (
            f"📢 <b>[거래소 신규 지정 종목] {latest}</b>\n"
            f"════════════\n\n"
            + "\n\n".join(sections)
        )
        stock_api.send_telegram(target, msg)
        logging.info(f"📢 [시장경보] 신규 지정 {len(new_alerts)}개 알림 발송")

    except Exception as e:
        logging.debug(f"시장경보 체크 오류: {e}")


@_job()
def job_collect_market_closing():
    """평일 장 마감 후 (15:40) — 전체 상장사 시장 데이터 수집"""
    if market_timer.is_kr_holiday():  # 주말/공휴일 스킵
        logging.info("⏸ [시장수집-전체] 주말/공휴일 스킵")
        return
    if not _COLLECTOR_OK:
        return
    try:
        logging.info("📊 [시장수집-전체] 장 마감 후 전체 상장사 수집 시작")
        ok, fail = run_market(all_listed=True)
        logging.info(f"📊 [시장수집-전체] 완료: 성공 {ok}개, 실패 {fail}개")
        _log_notice("system", f"[시장수집-전체] 완료 ({ok}개)")

        # 수집 완료 후 기간별 수익률 일괄 계산
        try:
            from collect_market import calculate_returns
            _sb_ret = _bridge._get_client() if _BRIDGE_OK else None
            if _sb_ret:
                calculate_returns(_sb_ret)
        except Exception as _re:
            logging.error(f"❌ [수익률] 계산 오류: {_re}")
    except Exception as e:
        logging.error(f"❌ [시장수집-전체] 오류: {e}")

    # 시장 수집 완료 후 관심가/목표가 도달 알림 (장 마감 기준)
    job_watchlist_alert()

    # 거래소 신규 지정 종목 알림 (장 마감 후 1회)
    _check_market_warnings()


@_job(holiday=True)
def job_short_surge():
    """평일 17:05 — KRX 공매도 비중 수집 + 5거래일 평균 대비 2배 급증 종목 텔레그램 알림"""
    if not _COLLECTOR_OK:
        logging.warning("[공매도급증] 수집 모듈 없음 — 스킵")
        return
    try:
        # 1. 당일 공매도 데이터 수집
        saved, _ = run_short()
        logging.info(f"📉 [공매도급증] 수집 완료 {saved}건")

        # 2. 급증 탐지
        if not _BRIDGE_OK:
            return
        sb = _bridge._get_client()
        if not sb:
            return
        surges = _check_short_surge(sb, n_days=5, multiplier=2.0)
        if not surges:
            logging.info("📉 [공매도급증] 급증 종목 없음")
            return

        # 3. 메인 채널 알림 (collect_short 공용 포매터)
        msg = _format_surge_msg(surges, multiplier=2.0)
        stock_api.send_telegram(DEFAULT_CHAT_ID, msg)
        logging.info(f"📉 [공매도급증] 알림 발송 완료 ({len(surges)}건)")
    except Exception as e:
        logging.error(f"❌ [공매도급증] job 실패: {e}")


@_job(holiday=True)
def job_collect_investor_trend():
    """평일 장 마감 후 (16:45) — 종목별 외국인·기관 순매수 확정 수집 (모니터링 종목).
    inquire-investor(FHKST01010900)로 market_data.foreign_net_buy/institution_net_buy 갱신.
    일별 quote 수집(frgn_ntby_qty)이 장중 추정치라 마감 후 0으로 비는 문제를 근본 해결.
    sector_daily_summary(17:15) 집계 전에 실행해 같은 날 수급이 반영되도록 한다."""
    if not _COLLECTOR_OK:
        logging.warning("[투자자수급] 수집 모듈 없음 — 스킵")
        return
    try:
        import collect_market
        updated, failed = collect_market.collect_investor_trend(all_listed=False)
        logging.info(f"=== [투자자수급] 완료: {updated}개 갱신 / 미수집 {failed} ===")
    except Exception as e:
        logging.error(f"❌ [투자자수급] 오류: {e}")


@_job(holiday=True)
def job_sector_summary():
    """평일 장 마감 후 (17:15) — 산업별 일별 요약 집계 (sector_daily_summary)"""
    try:
        logging.info("📊 [섹터요약] collect_sector_summary 실행")
        from collect_sector_summary import run as run_sector
        run_sector()
        logging.info("✅ [섹터요약] 완료")
    except Exception as e:
        logging.error(f"❌ [섹터요약] 오류: {e}")


@_job(holiday=True)
def job_collect_estimates():
    """평일 장 마감 후 (18:40) — KIS 종목추정실적(미래 매출/영업이익) 수집.
    consensus_estimates 스냅샷 누적 + est_date 변경 시 estimate_revisions 기록."""
    try:
        logging.info("📈 [추정실적] collect_estimates 실행 시작")
        import collect_estimates
        covered, revisions = collect_estimates.run()
        logging.info(f"=== [추정실적] 완료: 커버 {covered}종목 / 갱신감지 {revisions}건 ===")
    except Exception as e:
        logging.error(f"❌ [추정실적] 오류: {e}")


@_job(holiday=True)
def job_collect_credit_balance():
    """평일 오전 (10:30) — KOFIA 신용공여 잔고(신용거래융자 등) 수집.
    전 영업일분이 다음날 오전 발표되므로 14일 윈도 멱등 upsert."""
    try:
        logging.info("💳 [신용잔고] collect_credit_balance 실행 시작")
        import collect_credit_balance
        n = collect_credit_balance.run()
        logging.info(f"=== [신용잔고] 완료: {n}건 upsert ===")
        try:
            import credit_chart
            credit_chart.run()   # 신규 기준일이 있을 때만 본방 차트 발송
        except Exception as e:
            logging.error(f"❌ [신용잔고차트] 발송 오류: {e}")
    except Exception as e:
        logging.error(f"❌ [신용잔고] 오류: {e}")


@_job(holiday=True)
def job_leading_stocks():
    """평일 장 마감 후 (17:30) — 주도주 탐색기 스코어 계산 및 저장"""
    try:
        logging.info("🚀 [주도주] leading_stocks_generator 실행 시작")
        from leading_stocks_generator import run as run_leading
        run_leading()
        logging.info("✅ [주도주] 생성 완료")
    except Exception as e:
        logging.error(f"❌ [주도주] 생성 오류: {e}")


@_job(holiday=True)
def job_market_summary():
    """평일 장 마감 후 (18:30) — 투자포인트 요약 생성 (market_investment_summary).
    18:15 수급 확정 정산 이후 실행 — 프론트 '오늘의 시장 판단' Zone B의 DB 경로를 채운다."""
    try:
        logging.info("📝 [시장요약] market_summary_generator 실행")
        from market_summary_generator import run as run_summary
        run_summary()
        logging.info("✅ [시장요약] 완료")
    except Exception as e:
        logging.error(f"❌ [시장요약] 오류: {e}")


@_job()
def job_watchlist_alert():
    """
    장 마감 후 — watchlist 종목의 관심가/목표가 도달 여부 체크 & 개별 알림.

    체크 조건:
      - watch_price:  현재가 ≤ 관심가 (매수 고려 구간 진입)
      - target_price: 현재가 ≥ 목표가 × 0.95 (목표가 5% 이내 근접 또는 도달)

    알림 발송 대상:
      - DEFAULT_CHAT_ID (메인 채팅방)
      - 해당 종목 전용 채팅방 (COMPANY_CHAT_IDS에 있을 경우)

    중복 방지:
      - app_config의 watchlist_alerted_today 키에 오늘 알림 발송한
        (stock_code, alert_type) 세트를 저장. 당일 중복 발송 방지.
    """
    if not _BRIDGE_OK:
        return

    today = datetime.date.today().isoformat()

    try:
        sb = _bridge._get_client()

        # ── 오늘 이미 발송한 알림 목록 조회 ──
        alerted_today = set()
        try:
            cfg_res = sb.table('app_config') \
                        .select('value').eq('key', 'watchlist_alerted_today').single().execute()
            if cfg_res.data:
                raw = json.loads(cfg_res.data['value'])
                # 오늘 날짜 것만 유지
                if raw.get('date') == today:
                    alerted_today = set(tuple(x) for x in raw.get('alerted', []))
        except Exception:
            pass  # 없으면 빈 세트로 시작

        # ── watchlist 전체 조회 ──
        wl_res = sb.table('watchlist') \
                   .select('stock_code,corp_name,watch_price,target_price,group_name') \
                   .execute()
        watchlist = [
            w for w in (wl_res.data or [])
            if w.get('watch_price') or w.get('target_price')
        ]
        if not watchlist:
            return

        # ── 최신 market_data 조회 (오늘 또는 최근 거래일) ──
        date_res = sb.table('market_data') \
                     .select('base_date').order('base_date', desc=True).limit(1).execute()
        max_date = (date_res.data or [{}])[0].get('base_date')
        if not max_date:
            return

        codes = list({w['stock_code'] for w in watchlist if w.get('stock_code')})
        mkt_res = sb.table('market_data') \
                    .select('stock_code,price,price_change_rate') \
                    .eq('base_date', max_date) \
                    .in_('stock_code', codes) \
                    .execute()
        price_map = {r['stock_code']: r for r in (mkt_res.data or [])}

        # ── 도달 여부 체크 ──
        alerts = []  # [(stock_code, alert_type, msg)]

        for w in watchlist:
            code  = w.get('stock_code')
            name  = w.get('corp_name', code)
            mkt   = price_map.get(code)
            if not mkt or not mkt.get('price'):
                continue

            price = mkt['price']
            chg   = mkt.get('price_change_rate', 0) or 0
            chg_str = fmt_change_pct(chg)

            # 관심가 도달: 현재가 ≤ 관심가
            watch_p = w.get('watch_price')
            if watch_p and price <= watch_p:
                key = (code, 'watch')
                if key not in alerted_today:
                    gap = (watch_p - price) / watch_p * 100
                    msg = (
                        f"🔔 <b>[관심가 도달] {name}</b>\n"
                        f"현재가 {price:,.0f}원 ({chg_str})\n"
                        f"관심가 {watch_p:,.0f}원 — {gap:.1f}% 하회\n"
                        f"📈 <a href='https://finance.naver.com/item/main.nhn?code={code}'>네이버 금융</a>"
                    )
                    alerts.append((code, name, 'watch', msg))
                    alerted_today.add(key)

            # 목표가 근접: 현재가 ≥ 목표가 × 0.95
            target_p = w.get('target_price')
            if target_p and price >= target_p * 0.95:
                key = (code, 'target')
                if key not in alerted_today:
                    gap = (price - target_p) / target_p * 100
                    reached = price >= target_p
                    label = "도달 🎯" if reached else f"근접 ({abs(gap):.1f}% 이내)"
                    msg = (
                        f"{'🎯' if reached else '⚠️'} <b>[목표가 {label}] {name}</b>\n"
                        f"현재가 {price:,.0f}원 ({chg_str})\n"
                        f"목표가 {target_p:,.0f}원\n"
                        f"📈 <a href='https://finance.naver.com/item/main.nhn?code={code}'>네이버 금융</a>"
                    )
                    alerts.append((code, name, 'target', msg))
                    alerted_today.add(key)

        # ── 알림 발송 ──
        if alerts:
            target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)

            for code, name, alert_type, msg in alerts:
                stock_api.send_telegram(target, msg)
                logging.info(f"📢 [관심가알림] {name} ({alert_type}) → {target}")

            # 오늘 알림 목록 저장 (중복 방지)
            sb.table('app_config').upsert({
                'key':         'watchlist_alerted_today',
                'value':       json.dumps({
                    'date':    today,
                    'alerted': [list(k) for k in alerted_today],
                }, ensure_ascii=False),
                'description': f'{today} 관심가/목표가 알림 발송 이력'
            }, on_conflict='key').execute()

        logging.info(
            f"📋 [관심가알림] 체크 완료 — {len(watchlist)}개 종목 중 {len(alerts)}개 발송"
        )

    except Exception as e:
        logging.error(f"❌ [관심가알림] 오류: {e}")
