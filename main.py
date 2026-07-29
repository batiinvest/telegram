# main.py — DART 공시 봇 (개선 버전)
#
# 개선 사항:
#   1. 공시 중요도 3단계 분류 → 채널별 라우팅
#   2. 노이즈성 공시 스킵 (산업/메인 채널에서만)
#   3. 기업 블랙리스트 (app_config에서 관리)
#   4. 스킵/블랙리스트 DB 로드 지원

import re
import html
import datetime
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from managers import market_timer, HistoryManager, get_session
from db_utils import fetch_all_pages as _fetch_all_pages

import stock_api
from ai_analyst import analyze_disclosure_gemini
from dart_parser import get_disclosure_detail, get_audit_opinion

from config import (
    DART_API_KEY,
    TELEGRAM_BOT_TOKEN,
    INDUSTRY_CHAT_IDS,
    INDUSTRY_HIERARCHY,
    COMPANY_CHAT_IDS,
    CHAT_IDS_BY_CODE,
    COMPANY_CODES,
    AI_TRIGGER_KEYWORDS,
    GLOBAL_IMPORTANT_KEYWORDS,
    COMPANY_TO_INDUSTRY
)

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except Exception:
    _BRIDGE_OK = False

DEFAULT_CHAT_ID = "@BatiInvestChat"

# 분류·라우팅 정책은 dart_rules.py로 분리 (순수 함수 — test_dart_rules.py로 회귀 테스트).
# 키워드·필터 컨테이너는 in-place 갱신이라 from-import 참조 안전.
# reload_flag 시 재로드 콜백(config.on_reload)은 dart_rules 모듈이 등록.
from dart_rules import (
    classify_disclosure, decide_targets, load_dart_filters,
    DART_BLACKLIST, DART_TITLE_FILTER, DART_CORP_FILTER,
    CAP_MAIN, CAP_LARGE,
)

class DartRoutingBot:
    def __init__(self):
        self.base_url = "https://opendart.fss.or.kr/api/list.json"
        self.history  = HistoryManager("sent_list.txt", max_len=2000)
        self.ai_executor = ThreadPoolExecutor(max_workers=2)
        self.session = get_session()
        self._api_fail_streak = 0       # list.json 연속 실패 카운트 (관리자 알림용)
        self._swept = False             # 기동 첫 사이클 복구 스윕 완료 여부
        # 발송 실패 채널 재시도 큐: rcept_no → {targets, msg, n, desc}
        # (구 _retry_counts: 전 채널 실패만 재시도 → 부분 실패 채널 유실 + 재파싱 낭비)
        self._pending: dict = {}
        # 일일 운영 통계 (19시 이후 요약 1회 보고)
        self._day_stats: dict = {"date": "", "reported": False, "sent": 0,
                                 "filtered": 0, "fail": 0, "err": 0, "by_level": {}}

        # 시총 캐시 (메인 채널 필터링용)
        self._cap_cache: dict = {}   # stock_code(숫자) → market_cap
        self._cap_loaded: datetime.datetime | None = None
        self._load_cap_cache()

        # DB에서 키워드·필터 로드
        load_dart_filters()

        # 발송이력 DB 이중화 — sent_list.txt 유실(서버 이전 등) 시 당일 재발송 방지
        self._seed_history_from_db()

    def _load_cap_cache(self):
        """Supabase market_data에서 최신 시총 캐시 로드 (24시간마다 갱신).
        ※ 전체 상장사(2000개+) 처리를 위해 페이지네이션 사용."""
        if not _BRIDGE_OK:
            return
        try:
            sb = _bridge._get_client()
            date_res = sb.table('market_data').select('base_date') \
                         .order('base_date', desc=True).limit(1).execute()
            max_date = (date_res.data or [{}])[0].get('base_date')
            if max_date:
                all_rows = _fetch_all_pages(
                    sb.table('market_data')
                      .select('stock_code,market_cap')
                      .eq('base_date', max_date)
                )

                self._cap_cache = {
                    m['stock_code']: m['market_cap']
                    for m in all_rows
                    if m.get('market_cap') is not None
                }
                self._cap_loaded = datetime.datetime.now()
                logging.info(f"[공시봇] 시총 캐시 로드 완료: {len(self._cap_cache)}개")
        except Exception as e:
            logging.warning(f"[공시봇] 시총 캐시 로드 실패: {e}")

    def _get_cap(self, stock_code: str):
        """시총 조회 (캐시, 24시간 갱신). 정보 없으면 None."""
        if self._cap_loaded is None or \
           (datetime.datetime.now() - self._cap_loaded).total_seconds() > 86400:
            self._load_cap_cache()
            if self._cap_loaded is None or \
               (datetime.datetime.now() - self._cap_loaded).total_seconds() > 86400:
                # 갱신 실패 — 다음 주기까지 재시도 억제 (항목마다 블로킹 방지)
                self._cap_loaded = datetime.datetime.now()

        if not stock_code:
            return None
        code = stock_code.replace('.KS', '').replace('.KQ', '').strip()
        return self._cap_cache.get(code)

    def _seed_history_from_db(self):
        """notice_history의 최근 2일 발송분(#rcept_no 마커)으로 이력 복원.
        파일 이력이 정상이면 전부 no-op, 유실 시에만 실제 시드됨."""
        if not _BRIDGE_OK:
            return
        try:
            client = _bridge._get_client()
            if not client:
                return
            since = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=2)).isoformat()
            res = client.table('notice_history').select('content') \
                        .gte('created_at', since).like('content', '[공시/%') \
                        .limit(1000).execute()
            seeded = 0
            for row in (res.data or []):
                m = re.search(r'#(\d{14})\s*$', row.get('content') or '')
                if m and not self.history.contains(m.group(1)):
                    self.history.add(m.group(1))
                    seeded += 1
            if seeded:
                logging.info(f"[공시봇] 발송이력 {seeded}건 DB에서 복원 (파일 유실 대비)")
        except Exception as e:
            logging.warning(f"[공시봇] 발송이력 DB 시드 실패 (파일 이력만 사용): {e}")

    @staticmethod
    def _fmt_eok(won) -> str:
        """원 단위 정수 → '549억' / '-70억' / '1.6조' 압축 표기."""
        if won is None:
            return '-'
        try:
            n = int(won)
        except (ValueError, TypeError):
            return '-'
        sign = '-' if n < 0 else ''
        a = abs(n)
        if a >= 1_000_000_000_000:      # 조
            return f'{sign}{a / 1_000_000_000_000:.1f}조'
        if a >= 100_000_000:            # 억
            return f'{sign}{round(a / 100_000_000):,}억'
        return f'{sign}{round(a / 10_000):,}만'

    def _earnings_trend(self, stock_code: str, rcept_no: str = "", n: int = 5) -> str:
        """최근 n개 분기 매출/영업익/순이익 추이. 이번 분기(잠정)는 financials에
        아직 없어 공시 KV에서 뽑아 맨 위에 '(잠정실적)' 표기로 붙이고, 직전 분기는
        financials(is_cumulative=False = 탈누적 단독값)에서. 데이터 없으면 빈 문자열."""
        if not _BRIDGE_OK or not stock_code:
            return ''
        try:
            code = stock_code.split('.')[0]

            # 이번 분기(잠정) — 공시 KV에서 구조화 추출
            cur = None
            if rcept_no:
                try:
                    from dart_doc import _fetch_html, _build_kv
                    from dart_parser import extract_preliminary_current
                    html = _fetch_html(rcept_no)
                    cur = extract_preliminary_current(_build_kv(html)) if html else None
                except Exception:
                    logging.exception(f"⚠️ [공시] 잠정 현분기 추출 실패: {rcept_no}")

            sb = _bridge._get_client()
            if not sb:
                return ''
            rows = (sb.table('financials')
                    .select('bsns_year,quarter,revenue,operating_profit,net_income')
                    .eq('stock_code', code).eq('is_cumulative', False)
                    .order('bsns_year', desc=True).order('quarter', desc=True)
                    .limit(n).execute().data or [])
            if not rows and not (cur and cur.get('label')):
                return ''

            def _row(label, rev, op, net, tag=''):
                return (f"  {label}: {self._fmt_eok(rev)} / {self._fmt_eok(op)} / "
                        f"{self._fmt_eok(net)}{tag}")

            lines = ['📈 최근 실적 (매출/영업익/순이익)']
            cy, cq = (cur or {}).get('year'), (cur or {}).get('quarter')
            if cur and cur.get('label'):
                lines.append(_row(cur['label'], cur.get('revenue'),
                                  cur.get('operating_profit'), cur.get('net_income'),
                                  ' (잠정실적)'))
            for r in rows:
                # 잠정으로 이미 표시한 분기가 financials에도 있으면 중복 제거
                if cy and r.get('bsns_year') == cy and str(r.get('quarter')) == f'Q{cq}':
                    continue
                yy = str(r.get('bsns_year', ''))[2:]
                qn = str(r.get('quarter', '')).replace('Q', '').strip()
                lines.append(_row(f'{yy}.{qn}Q', r.get('revenue'),
                                  r.get('operating_profit'), r.get('net_income')))
                if len(lines) - 1 >= n:
                    break
            return '\n'.join(lines) if len(lines) > 1 else ''
        except Exception:
            logging.exception(f"⚠️ [공시] 실적추이 조회 실패: {stock_code}")
            return ''

    def ai_worker(self, chat_id, corp_name, report_nm, rcept_no):
        logging.info(f"🤖 AI Analyzing: {corp_name}")
        result = analyze_disclosure_gemini(corp_name, report_nm, rcept_no)
        if result:
            # AI 출력·기업명·공시명은 <,>,& 포함 가능 → HTML parse_mode 보호 이스케이프
            corp_esc   = html.escape(corp_name or "", quote=False)
            report_esc = html.escape(re.sub(r'\s+', ' ', report_nm).strip(), quote=False)
            result_esc = html.escape(result.strip(), quote=False)
            msg = (
                f"🤖 <b>[AI 심층 분석] {corp_esc}</b>\n"
                f"└ {report_esc}\n\n{result_esc}\n\n"
                f"⚠️ <i>AI 분석은 참고용이며 투자 판단의 근거가 아닙니다.</i>"
            )
            stock_api.send_telegram(chat_id, msg)

    def get_emoji(self, title: str) -> str:
        t = title.replace(" ", "")
        if any(x in t for x in ["거래정지", "횡령", "배임", "소송", "불성실", "상장폐지"]): return "🚨"
        if any(x in t for x in ["공급계약", "수주", "무상증자", "최대주주변경", "공개매수"]): return "📈"
        if any(x in t for x in ["유상증자", "전환사채", "CB", "BW", "신주인수권"]): return "💰"
        if any(x in t for x in ["주식등의대량보유", "임원", "주요주주", "소유상황"]): return "📊"
        if any(x in t for x in ["사업보고서", "분기보고서", "잠정실적"]): return "📘"
        return "📄"

    def _get_company_chat_id(self, corp_name: str, stock_code: str = "") -> str | None:
        """stock_api.get_company_chat_id()로 위임 (모든 파일 공통 사용)"""
        return stock_api.get_company_chat_id(corp_name, stock_code)

    def _build_msg(self, corp_name, report_nm, rcept_no, stock_code, prefix="", detail=""):
        emoji       = self.get_emoji(report_nm)
        # 배지와 같은 이모지면 생략 ("🚨[긴급] 🚨[기업명]" 중복 방지)
        if emoji and emoji in prefix:
            emoji = ""
        link        = f"http://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
        target_code = COMPANY_CODES.get(corp_name, stock_code)
        price_info  = stock_api.get_stock_price(target_code)
        stock_msg   = f"<b>{price_info}</b>\n" if price_info else ""
        # 외부 데이터(DART 파싱 detail·기업명·공시명)는 원문 XML/특수문자(<,>,&)를
        # 포함할 수 있어 HTML parse_mode를 깨뜨림(예: <?xml → 400 발송실패) → 이스케이프.
        corp_esc    = html.escape(corp_name or "", quote=False)
        detail_esc  = html.escape(detail, quote=False) if detail else ""
        detail_block = f"\n\n{detail_esc}" if detail_esc else ""
        # DART report_nm에 과도한 공백이 포함되는 경우 정규화
        report_nm_clean = html.escape(re.sub(r'\s+', ' ', report_nm).strip(), quote=False)
        head = f"{prefix}{emoji} " if emoji else prefix
        return (
            f"{head}<b>[{corp_esc}]</b>\n"
            f"{stock_msg}{report_nm_clean}{detail_block}\n"
            f"🔗 <a href='{link}'>공시 원문</a> | "
            f"📈 <a href='https://finance.naver.com/item/main.nhn?code={target_code}'>네이버</a>"
        )

    _MAX_PAGES         = 3    # 평시 캡 — 100건×3페이지, 분당 300건 폭주까지 커버
    _MAX_PAGES_STARTUP = 30   # 기동 첫 사이클 캡 — 중단·장애 시간대 복구 스윕용

    def _note_api_failure(self, detail: str):
        """list.json 오류 로그 + 연속 실패 누적 시 관리자 1회 알림.
        (구: 오류 status 무로그 break — 쿼터초과·키만료로 수집이 전면
         중단돼도 heartbeat는 정상이라 탐지 불가했음)"""
        self._api_fail_streak += 1
        logging.error(f"❌ [공시] list.json 오류 ({self._api_fail_streak}연속): {detail}")
        if self._api_fail_streak == 10:
            try:
                from telegram_utils import get_admin_chat_id
                admin = get_admin_chat_id()
                if admin:
                    stock_api.send_telegram(
                        admin, f"🚨 <b>[공시봇]</b> DART 수집 10분 연속 실패\n└ {detail}")
            except Exception:
                logging.exception("⚠️ [공시] 관리자 알림 발송 실패")

    def _fetch_items(self, now) -> list:
        """DART list.json 조회 (어제~오늘, 최신순).

        - bgn_de=어제: 19시 폴링 종료 이후 접수분(저녁 정정·5%보고 등)을
          다음날 아침 첫 사이클이 자연 수거 (구: '오늘' 고정 → 영구 누락).
        - 페이지는 최신순 → 이미 처리한 공시가 포함된 페이지까지만 조회
          (그보다 오래된 페이지는 기처리 영역).
        - 기동 첫 사이클은 캡 30페이지: 재기동·장애 시간대 복구 스윕.
        """
        bgn_de = (now - datetime.timedelta(days=1)).strftime("%Y%m%d")
        end_de = now.strftime("%Y%m%d")
        max_pages = self._MAX_PAGES if self._swept else self._MAX_PAGES_STARTUP
        self._swept = True

        items = []
        total_page = 1
        for _page in range(1, max_pages + 1):
            params = {
                "crtfc_key": DART_API_KEY,
                "bgn_de": bgn_de, "end_de": end_de,
                "page_count": 100, "page_no": _page,
            }
            res = self.session.get(self.base_url, params=params, timeout=10)
            try:
                data = res.json()
            except ValueError:
                self._note_api_failure(f"비JSON 응답 (HTTP {res.status_code})")
                break
            status = data.get("status")
            if status not in ("000", "013"):   # 013 = 조회 데이터 없음 (정상)
                self._note_api_failure(f"status={status} {data.get('message', '')}")
                break
            self._api_fail_streak = 0
            if status == "013":
                break
            page_items = data.get("list", [])
            items.extend(page_items)
            total_page = int(data.get("total_page", 1) or 1)
            if _page >= total_page:
                break
            if any(self.history.contains(it.get("rcept_no")) for it in page_items):
                break
        else:
            logging.warning(f"⚠️ [공시] 페이지 캡({max_pages}) 도달 — "
                            f"미조회분 존재 가능 (total_page={total_page})")
        return items

    def _process_item(self, item: dict):
        """공시 1건 처리: 필터 → 분류 → 메시지 → 라우팅 발송 → 이력 기록.
        run() 사이클에서 항목별 try로 감싸 호출 — 한 항목의 예외가
        같은 사이클의 나머지(더 최신) 공시 처리를 막지 않도록 격리.
        반환: 'sent' | 'fail'(전채널 실패) | 'filtered' | 'ignored' | None(기처리)."""
        rcept_no   = item.get("rcept_no") or ""
        corp_name  = item.get("corp_name") or ""
        report_nm  = item.get("report_nm") or ""
        stock_code = (item.get("stock_code") or "").strip()

        # ① 처리 이력
        if not rcept_no or self.history.contains(rcept_no):
            return None

        # ② 비상장 제외
        if not stock_code:
            return None

        # ③ 블랙리스트 제외 (정확한 기업명)
        if corp_name in DART_BLACKLIST:
            self.history.add(rcept_no)
            return 'filtered'

        # ③-a 기업명 부분일치 필터
        if DART_CORP_FILTER and any(k in corp_name for k in DART_CORP_FILTER):
            self.history.add(rcept_no)
            return 'filtered'

        # ③-b 공시 제목 필터
        if DART_TITLE_FILTER and any(k in report_nm for k in DART_TITLE_FILTER):
            self.history.add(rcept_no)
            return 'filtered'

        is_my_stock         = (corp_name in COMPANY_CODES) or (bool(stock_code) and stock_code.split(".")[0] in CHAT_IDS_BY_CODE)
        is_global_important = any(k in report_nm for k in GLOBAL_IMPORTANT_KEYWORDS)

        # ④ 내 종목도 아니고 전체 중요 공시도 아니면 스킵
        if not is_my_stock and not is_global_important:
            return 'ignored'

        # ⑤ 공시 중요도 분류
        level = classify_disclosure(report_nm)

        # ⑤-a 감사·검토보고서 승격: 제목만으론 비적정 의견 감지 불가(잡공시 등급) →
        # 원문에서 감사의견 추출, 한정/부적정/의견거절이면 긴급 승격
        audit_note = ""
        if '감사보고서' in report_nm or '검토보고서' in report_nm:
            try:
                opinion = get_audit_opinion(rcept_no)
            except Exception:
                logging.exception(f"⚠️ [공시] 감사의견 확인 실패: {corp_name}")
                opinion = None
            if opinion and opinion != '적정':
                level = 'urgent'
                audit_note = f"🚨 감사의견: {opinion}"
                logging.warning(f"🚨 [공시] 감사의견 비적정({opinion}) → 긴급 승격: {corp_name}")

        # ── 메시지 생성 ──
        is_market_wide = not is_my_stock and is_global_important
        # 배지는 하나만 — 시장속보와 등급을 겹쳐 붙이면 제목 앞이
        # "🔥[시장속보] 📌[주요] 📈" 3중이 돼 가독성이 떨어짐.
        # 시장속보 자체가 '전체 중요 공시'라 주요 배지는 흡수하고, 긴급만 병기.
        if is_market_wide:
            prefix = ("🚨 <b>[속보·긴급]</b> " if level == 'urgent'
                      else "🔥 <b>[시장속보]</b> ")
        elif level == 'urgent':
            prefix = "🚨 <b>[긴급]</b> "
        elif level == 'major':
            prefix = "📌 <b>[주요]</b> "
        else:
            prefix = ""
        detail = get_disclosure_detail(rcept_no, report_nm)
        if audit_note:
            detail = f"{audit_note}\n{detail}".strip()
        # 잠정실적 — 이번 분기(잠정)+직전 분기 추이 덧붙임
        if '잠정' in report_nm and stock_code:
            trend = self._earnings_trend(stock_code, rcept_no)
            if trend:
                detail = f"{detail}\n{trend}".strip()
        msg = self._build_msg(corp_name, report_nm, rcept_no, stock_code, prefix, detail)

        # ── 채널 라우팅 — 정책은 dart_rules.decide_targets (순수 함수) ──
        industry = COMPANY_TO_INDUSTRY.get(corp_name)
        _ind_cid = INDUSTRY_CHAT_IDS.get(industry) if industry else None
        _cid     = self._get_company_chat_id(corp_name, stock_code)
        cap      = self._get_cap(stock_code)

        targets = decide_targets(
            level,
            main_chat=DEFAULT_CHAT_ID,
            ind_chat=_ind_cid,
            comp_chat=_cid,
            is_market_wide=is_market_wide,
            report_nm=report_nm,
            cap_ok_main=(cap is None or cap >= CAP_MAIN),
            cap_ok_large=(cap is not None and cap >= CAP_LARGE),
        )

        results = [stock_api.send_telegram(t, msg) for t in targets]
        failed  = [t for t, ok in zip(targets, results) if not ok]

        # 실패 채널만 재시도 큐 등록 — 사이클마다 재발송, 최대 3회 (H-2)
        # 이력은 아래서 즉시 기록: 재파싱·성공 채널 중복 발송 없이 실패분만 복구
        if failed:
            if len(self._pending) < 50:   # 장애 폭주 시 무한 성장 방지
                self._pending[rcept_no] = {
                    "targets": failed, "msg": msg, "n": 0,
                    "desc": f"{corp_name} {report_nm[:30]}",
                }
            logging.warning(f"⚠️ [공시] {len(failed)}/{len(targets)}채널 발송 실패 "
                            f"— 재시도 큐 등록: {corp_name} {report_nm}")

        # ── AI 분석 (긴급/중요만) ── [임시 중지: 업데이트 후 재적용]
        # Gemini 모델 폐기로 인해 분석 실패 → 임시 비활성화 (2026-06-25)
        # 복구 시 아래 블록 주석 해제 (+ ai_worker 메시지 escape·호재/악재 표현 완화 필요)
        # if level in ('urgent', 'major') and \
        #    any(k in report_nm for k in AI_TRIGGER_KEYWORDS):
        #     logging.info(f"🤖 AI 분석 큐: {corp_name}")
        #     ai_target = self._get_company_chat_id(corp_name, stock_code) or DEFAULT_CHAT_ID
        #     self.ai_executor.submit(
        #         self.ai_worker, ai_target, corp_name, report_nm, rcept_no
        #     )

        # ── 발송 기록 ──
        if _BRIDGE_OK:
            try:
                # content 끝 '#rcept_no' 마커 = 발송이력 DB 이중화 키
                # (_seed_history_from_db가 재기동 시 파싱해 이력 복원)
                _bridge.log_notice(
                    target=corp_name,
                    content=f"[공시/{level}] {report_nm} #{rcept_no}",
                    sent_count=len(targets),
                    ok_count=sum(1 for r in results if r),
                )
            except Exception:
                pass

        self.history.add(rcept_no)
        logging.info(f"✅ [공시/{level}] {corp_name}: {report_nm}")
        bl = self._day_stats["by_level"]
        bl[level] = bl.get(level, 0) + 1
        time.sleep(1)
        return 'fail' if (targets and not any(results)) else 'sent'

    def _flush_retries(self):
        """발송 실패 채널 재시도 — 사이클당 1회, 항목당 최대 3회 후 포기."""
        for rcept_no in list(self._pending):
            ent = self._pending.get(rcept_no)
            if not ent:
                continue
            still = [t for t in ent["targets"] if not stock_api.send_telegram(t, ent["msg"])]
            ent["n"] += 1
            if not still:
                logging.info(f"✅ [공시] 재시도 성공 ({ent['n']}회차): {ent['desc']}")
                self._pending.pop(rcept_no, None)
            elif ent["n"] >= 3:
                logging.error(f"❌ [공시] 재시도 3회 실패 — 포기 ({len(still)}채널): {ent['desc']}")
                self._pending.pop(rcept_no, None)
            else:
                ent["targets"] = still
                logging.warning(f"⚠️ [공시] 재시도 {ent['n']}/3 — 잔여 {len(still)}채널: {ent['desc']}")
            time.sleep(1)

    def _report_daily(self):
        """일일 운영 요약 — 19시 이후 1회 로그 + (발송 있던 날만) 관리자 방 보고."""
        d = self._day_stats
        d["reported"] = True
        bl = d.get("by_level", {})
        parser_line = ""
        try:
            from dart_parser import PARSER_STATS
            cat = sum(v for k, v in PARSER_STATS.items()
                      if k not in ('fallback', 'empty', 'no_html', 'skip_type', 'error'))
            parser_line = (f"파서: 전용 {cat} · 범용 {PARSER_STATS.get('fallback', 0)}"
                           f" · 빈결과 {PARSER_STATS.get('empty', 0)}"
                           f" · 원문없음 {PARSER_STATS.get('no_html', 0)}"
                           f" · 오류 {PARSER_STATS.get('error', 0)}")
            PARSER_STATS.clear()
        except Exception:
            pass
        msg = (f"📊 <b>[공시봇 일일요약]</b> {d['date']}\n"
               f"발송 {d['sent']}건 (긴급 {bl.get('urgent', 0)} · 주요 {bl.get('major', 0)}"
               f" · 일반 {bl.get('normal', 0)} · 잡 {bl.get('skip', 0)})\n"
               f"필터제외 {d['filtered']} · 전채널실패 {d['fail']} · 처리오류 {d['err']}"
               f" · 재시도잔여 {len(self._pending)}"
               + (f"\n{parser_line}" if parser_line else ""))
        logging.info("[공시] 일일요약 | " + msg.replace("\n", " | "))
        if d["sent"] or d["fail"] or d["err"]:
            try:
                from telegram_utils import get_admin_chat_id
                admin = get_admin_chat_id()
                if admin:
                    stock_api.send_telegram(admin, msg)
            except Exception:
                logging.exception("⚠️ [공시] 일일요약 발송 실패")

    def run(self):
        logging.info("🚀 DART Bot Started")
        loop_count = 0

        while True:
            loop_count += 1
            if loop_count % 60 == 1 and _BRIDGE_OK:
                try:
                    _bridge.heartbeat("dart_bot")
                except Exception:
                    pass
                # reload_flag 소비는 watchdog 단일 창구 —
                # 필터 갱신은 config.on_reload(_load_dart_filters) 콜백으로 수신

            now = market_timer.get_now()

            # ── 일일 통계 리셋 / 19시 이후 요약 보고 ──
            today = now.strftime("%Y%m%d")
            if self._day_stats["date"] != today:
                self._day_stats = {"date": today, "reported": False, "sent": 0,
                                   "filtered": 0, "fail": 0, "err": 0, "by_level": {}}
            elif now.hour >= 19 and not self._day_stats["reported"]:
                try:
                    self._report_daily()
                except Exception:
                    logging.exception("⚠️ [공시] 일일요약 생성 실패")
                    self._day_stats["reported"] = True

            if not market_timer.is_weekday():
                for _ in range(60): time.sleep(60)
                continue

            if 7 <= now.hour < 19:
                try:
                    if self._pending:
                        self._flush_retries()
                    st = {"sent": 0, "fail": 0, "filtered": 0, "ignored": 0, "err": 0}
                    items = self._fetch_items(now)
                    for item in reversed(items):
                        try:
                            outcome = self._process_item(item)
                            if outcome:
                                st[outcome] = st.get(outcome, 0) + 1
                        except Exception:
                            st["err"] += 1
                            rn = item.get("rcept_no") or ""
                            logging.exception(
                                f"❌ [공시] 항목 처리 실패 — 스킵({rn}): "
                                f"{item.get('corp_name')} {str(item.get('report_nm', ''))[:40]}")
                            if rn:
                                self.history.add(rn)   # 무한 재시도 차단

                    # 사이클 요약 — 유효 이벤트(발송·제외·실패) 있을 때만 기록
                    if st["sent"] or st["fail"] or st["err"] or st["filtered"]:
                        logging.info(f"[공시] 사이클: 발송 {st['sent']} · 필터제외 {st['filtered']}"
                                     f" · 관심밖 {st['ignored']} · 전채널실패 {st['fail']}"
                                     f" · 오류 {st['err']} · 재시도대기 {len(self._pending)}")
                    d = self._day_stats
                    d["sent"] += st["sent"]; d["filtered"] += st["filtered"]
                    d["fail"] += st["fail"]; d["err"] += st["err"]
                    time.sleep(60)
                except Exception:
                    logging.exception("❌ [공시] 사이클 실패")
                    time.sleep(60)
            else:
                time.sleep(600)


if __name__ == "__main__":
    bot = DartRoutingBot()
    bot.run()
