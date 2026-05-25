import requests 
import datetime
import time
import logging
import threading
import uuid
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from managers import market_timer, HistoryManager
# ✅ 통합 API 모듈 및 설정
import stock_api  
from config import (
    TELEGRAM_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    COMMON_BUTTON,
    COMPANY_CODES,
    COMPANY_CHAT_IDS,
    INDUSTRY_CHAT_IDS,
    COMPANY_TO_INDUSTRY,
)

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except Exception:
    _BRIDGE_OK = False

# ══════════════════════════════════════════
#  시세 알림 임계값 (기본값 — DB에서 덮어씀)
# ══════════════════════════════════════════
ALERT_SURGE_THRESHOLD = 15.0   # 급등 기준 (%)
ALERT_UP_THRESHOLD    =  5.0   # 강세 기준 (%)
ALERT_DOWN_THRESHOLD  = -5.0   # 약세 기준 (%)

# 어드민 채팅방 (기본값 — DB에서 덮어씀)
ADMIN_CHAT_ID = "@batiinvest"

# 신뢰도 낮은 뉴스 출처 (기본값 — DB에서 덮어씀)
LOW_TRUST_SOURCES = ['blog.naver', 'cafe.naver', 'tistory', 'brunch', 'newspim', 'fntoday', 'edaily']


def _load_alert_config():
    """app_config에서 시세 알림 임계값, 어드민 채팅방, 신뢰도 낮은 출처 로드"""
    global ALERT_SURGE_THRESHOLD, ALERT_UP_THRESHOLD, ALERT_DOWN_THRESHOLD
    global ADMIN_CHAT_ID, LOW_TRUST_SOURCES
    if not _BRIDGE_OK:
        return
    try:
        client = _bridge._get_client()
        if not client:
            return
        keys = ['alert_threshold_surge', 'alert_threshold_up', 'alert_threshold_down',
                'admin_chat_id', 'news_low_trust_sources']
        res = client.table('app_config').select('key,value').in_('key', keys).execute()
        cfg = {r['key']: r['value'] for r in (res.data or [])}

        if cfg.get('alert_threshold_surge'):
            ALERT_SURGE_THRESHOLD = float(cfg['alert_threshold_surge'])
        if cfg.get('alert_threshold_up'):
            ALERT_UP_THRESHOLD = float(cfg['alert_threshold_up'])
        if cfg.get('alert_threshold_down'):
            ALERT_DOWN_THRESHOLD = float(cfg['alert_threshold_down'])
        if cfg.get('admin_chat_id'):
            ADMIN_CHAT_ID = cfg['admin_chat_id'].strip()
        if cfg.get('news_low_trust_sources'):
            LOW_TRUST_SOURCES = [s.strip() for s in cfg['news_low_trust_sources'].split(',') if s.strip()]

        logging.info(
            f"✅ [시세봇] 설정 로드 — "
            f"급등:{ALERT_SURGE_THRESHOLD}% 강세:{ALERT_UP_THRESHOLD}% "
            f"약세:{ALERT_DOWN_THRESHOLD}% 어드민:{ADMIN_CHAT_ID}"
        )
    except Exception as e:
        logging.warning(f"⚠️ [시세봇] 설정 로드 실패 (기본값 사용): {e}")


class KisMyStockScanner:
    def __init__(self):
        # 파일 기반 중복 방지 — 재시작해도 당일 발송 기록 유지
        self.sent_history = HistoryManager("sent_alert.txt", max_len=1000)
        self.last_update_id = 0
        self.listener_session = requests.Session()
        self.stock_memory = {}
        self.pending_reports = {} 
        self.input_wait_list = {}
        
        # ✅ [최적화] 스레드 풀 및 락 설정
        self.executor = ThreadPoolExecutor(max_workers=5)
        self._lock = threading.Lock()

        self.chat_id_to_code = {}
        self.chat_id_to_name = {}
        
        # DB에서 알림 임계값·어드민 채팅방·신뢰도 출처 로드
        _load_alert_config()

        # 설정 파일 로드
        for name, chat_id in COMPANY_CHAT_IDS.items():
            if name in COMPANY_CODES:
                str_cid = str(chat_id)
                self.chat_id_to_code[str_cid] = COMPANY_CODES[name]
                self.chat_id_to_name[str_cid] = name

        # ===============================================================
        # 🎮 [Refactor] Dictionary Dispatcher (명령어 매핑)
        # ===============================================================
        self.commands = {
            # 1. 도움말
            "/?": self.cmd_help,
            "/도움말": self.cmd_help,

            # 2. 산업방 전용
            "/업황": self.cmd_sector_status,
            "/자금": self.cmd_sector_funds,
            "/테마": self.cmd_theme_analysis,
            "/비교": self.cmd_sector_comparison,
            "/시총순": self.cmd_industry_cap_ranking,
            "/실적": self.cmd_industry_financial_ranking,

            # 3. 메인방 전용
            "/전광판": self.cmd_market_board,
            "/랭킹": self.cmd_ranking,

            # 4. 공용/특수
            "/섹터비교": self.cmd_compare_sectors,

            # 5. 종목 분석 (대상 종목 필요)
            "/주가": self.cmd_stock_price,
            "/수급": self.cmd_stock_supply,
            "/진단": self.cmd_stock_chart,
            "/차트": self.cmd_stock_chart,
            "/재무": self.cmd_stock_finance,
            "/브리핑": self.cmd_stock_briefing,
            "/일정": self.cmd_stock_briefing
        }

    # ===============================================================
    # 🕵️‍♂️ 내부 로직 (감시 루프)
    # ===============================================================
    def monitoring_loop(self):
        logging.info("🕵️‍♂️ 바티대리 감시 업무 시작 (09:00 ~ 15:30)")
        while True:
            try:
                # 1. 장 운영 시간 (08:50 ~ 15:30) - market_timer로 한 번에 체크
                if market_timer.is_market_open():
                    self.check_my_stocks()
                    time.sleep(30) # 장중에는 30초 주기

                # 2. 장 마감 후 및 주말
                else:
                    # 메모리 청소 (stock_memory만 — sent_history는 파일 기반이라 재시작 안전)
                    if self.stock_memory:
                        with self._lock:
                            self.stock_memory.clear()
                            logging.info("🌙 업무 마감/자정: 메모리 초기화 완료")

                    # (2) 야간/주말 절전 모드 최적화
                    # 현재 시간 가져오기 (KST 기준)
                    now_hour = market_timer.get_now().hour

                    # 주말이거나, 16시 이후 또는 07시 이전이면 '푹' 잠 (1시간)
                    if not market_timer.is_weekday() or (now_hour >= 16 or now_hour < 7):
                        time.sleep(3600)
                    else:
                        # 07시~08:50 사이 평일 아침에는 대기 (1분)
                        time.sleep(60)

            except Exception as e:
                logging.error(f"Monitor Loop Error: {e}")
                time.sleep(60)

    def check_my_stocks(self):
        targets = [name for name in COMPANY_CHAT_IDS.keys() if name in COMPANY_CODES]
        future_to_name = {
            self.executor.submit(self._check_single_stock, name): name 
            for name in targets
        }
        for future in as_completed(future_to_name):
            try: future.result()
            except Exception: pass

    def _check_single_stock(self, name):
        raw_code = COMPANY_CODES[name]
        chat_id = COMPANY_CHAT_IDS[name]
        
        data = stock_api.get_raw_price(raw_code)
        if not data: return
        
        try:
            price = int(data['stck_prpr'])
            rate = float(data['prdy_ctrt'])
            status_code = data.get('iscd_stat_cls_code', '00').strip()
            upper = int(data['stck_mxpr'])
            lower = int(data['stck_llam'])

            if status_code in ['58', '59']:
                 self._send_once(f"{raw_code}_VI", chat_id, f"🧊 <b>[{name} VI 발동]</b>\n변동성 완화 장치 발동!\n💰 현재가: {price:,}원 ({rate}%)")
            elif price >= upper:
                self._send_once(f"{raw_code}_UPPER", chat_id, f"🛑 <b>[{name} 상한가]</b>\n문 닫았습니다! (+{rate}%)\n💰 현재가: {price:,}원")
            elif price <= lower:
                self._send_once(f"{raw_code}_LOWER", chat_id, f"😱 <b>[{name} 하한가]</b>\n대응 확인! ({rate}%)\n💰 현재가: {price:,}원")
            elif rate >= ALERT_SURGE_THRESHOLD:
                self._send_once(f"{raw_code}_UP_15", chat_id, f"🚀 <b>[{name} 급등]</b>\n수급 유입 (+{rate}%)\n💰 현재가: {price:,}원")
            elif rate >= ALERT_UP_THRESHOLD:
                self._send_once(f"{raw_code}_UP_5", chat_id, f"📈 <b>[{name} 강세]</b>\n{ALERT_UP_THRESHOLD}% 이상 상승 중 (+{rate}%)\n💰 현재가: {price:,}원")
            elif rate <= ALERT_DOWN_THRESHOLD:
                self._send_once(f"{raw_code}_DOWN_5", chat_id, f"📉 <b>[{name} 약세]</b>\n{abs(ALERT_DOWN_THRESHOLD)}% 이상 하락 중 ({rate}%)\n💰 현재가: {price:,}원")
                
        except Exception as e:
            logging.error(f"Logic Error in {name}: {e}")

    def _send_once(self, key, chat_id, msg):
        with self._lock:
            if not self.sent_history.contains(key):
                stock_api.send_telegram(chat_id, msg)
                self.sent_history.add(key)

    # ===============================================================
    # 🛠️ [Helper] 제보 및 관리자 기능 (기존 로직 복원)
    # ===============================================================
    def delete_message(self, chat_id, message_id):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
            self.listener_session.post(url, json={"chat_id": chat_id, "message_id": message_id})
        except: pass 

    def send_report_approval(self, report_id, reporter, content):
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📅 일정 등록 (날짜)", "callback_data": f"REP|SCH|{report_id}"},
                    {"text": "💡 투자포인트(재료) 등록", "callback_data": f"REP|PNT|{report_id}"}
                ],
                [
                    {"text": "❌ 거절 (삭제)", "callback_data": f"REP|DEL|{report_id}"}
                ]
            ]
        }
        msg = (
            f"📩 <b>[새로운 제보 도착]</b>\n"
            f"👤 보낸이: {reporter}\n"
            f"📝 내용: {content}\n\n"
            f"어디에 등록하시겠습니까?"
        )
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        self.listener_session.post(url, json={"chat_id": ADMIN_CHAT_ID, "text": msg, "parse_mode": "HTML", "reply_markup": keyboard})

    def process_report(self, chat_id, message_id, user_name, text):
        self.delete_message(chat_id, message_id)
        content = text.replace("/제보", "").strip()
        if not content: return

        report_id = str(uuid.uuid4())[:8]
        self.pending_reports[report_id] = {
            "reporter": user_name,
            "content": content,
            "origin_chat": chat_id
        }
        self.send_report_approval(report_id, user_name, content)

    def process_admin_input(self, chat_id, text):
        if chat_id not in self.input_wait_list: return
        
        task = self.input_wait_list.pop(chat_id)
        target_tab = task['tab']
        origin_msg_id = task['origin_msg_id']
        
        if ',' in text: tokens = [t.strip() for t in text.split(',')]
        else: tokens = text.split(maxsplit=2)
        
        stock_name = ""; date_str = ""; note = ""; success = False
        # 기본 날짜: 오늘
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")

        if target_tab == "일정":
            if len(tokens) >= 3:
                stock_name = tokens[0]; date_str = tokens[1]; note = ", ".join(tokens[2:]) 
            elif len(tokens) == 2:
                stock_name = tokens[0]; date_str = today_str; note = tokens[1]
            else:
                stock_api.send_telegram(chat_id, "⚠️ 형식이 맞지 않습니다.")
                return
            success = stock_api.add_to_google_sheet("일정", [stock_name, date_str, note])

        else:
            # 투자포인트 로직
            if len(tokens) >= 2:
                stock_name = tokens[0]
                note = ", ".join(tokens[1:]) 
                date_str = today_str
            else:
                stock_api.send_telegram(chat_id, "⚠️ 형식이 맞지 않습니다.")
                return
            success = stock_api.update_investment_point(stock_name, note, date_str)
            
        result_msg = ""
        if success:
            result_msg = f"✅ <b>[{target_tab}]</b> 등록 완료!\n📝 <b>입력값:</b> {text}"
        else:
            result_msg = f"❌ 등록 실패 (API 오류)\n입력값: {text}"

        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        self.listener_session.post(edit_url, json={
            "chat_id": chat_id,
            "message_id": origin_msg_id,
            "text": result_msg,
            "parse_mode": "HTML"
        })

    # ===============================================================
    # 🛠️ [Helper] 컨텍스트 및 종목 파싱
    # ===============================================================
    def _get_chat_context(self, chat_id, matched_key):
        """채팅방의 종류(메인, 산업, 일반)와 현재 산업명을 반환"""
        is_main_room = (matched_key == DEFAULT_CHAT_ID) or (str(chat_id) == str(DEFAULT_CHAT_ID))
        current_industry = None
        
        if not is_main_room:
            # 1. ID로 찾기
            for ind_name, ind_id in INDUSTRY_CHAT_IDS.items():
                if str(ind_id) == str(chat_id):
                    current_industry = ind_name
                    break
            # 2. 유저네임(@...)으로 찾기
            if not current_industry and matched_key:
                for ind_name, ind_id in INDUSTRY_CHAT_IDS.items():
                    if str(ind_id).lower() == str(matched_key).lower():
                        current_industry = ind_name
                        break
        
        return {
            "is_main": is_main_room,
            "is_industry": (current_industry is not None),
            "industry_name": current_industry,
            "matched_key": matched_key
        }

    def _resolve_target_stock(self, args, matched_key):
        target_code = None
        target_name = ""

        # 1. 인자 O
        if len(args) >= 1:
            input_name = args[0]
            if input_name in COMPANY_CODES:
                target_code = COMPANY_CODES[input_name]
                target_name = input_name
                return target_code, target_name
            else:
                return None, input_name 

        # 2. 인자 X, 종목방 O
        if matched_key and matched_key in self.chat_id_to_code:
            target_code = self.chat_id_to_code[matched_key]
            target_name = self.chat_id_to_name[matched_key]
            return target_code, target_name

        return None, None

    # ===============================================================
    # 🛠️ [Refactor] 공통 실행 헬퍼 (반복 코드 제거기)
    # ===============================================================
    def _exec_industry_cmd(self, chat_id, ctx, wait_msg, api_func, *args):
        """산업방 명령어 실행을 위한 공통 래퍼"""
        if not ctx["is_industry"]:
            stock_api.send_telegram(chat_id, "⚠️ <b>산업별 채팅방</b> 전용 명령어입니다.")
            return
        
        # 대기 메시지가 있는 경우만 전송
        if wait_msg:
            stock_api.send_telegram(chat_id, wait_msg)
            
        # API 호출 및 결과 전송
        msg = api_func(ctx["industry_name"], *args)
        stock_api.send_telegram(chat_id, msg,keyboard=COMMON_BUTTON)

    def _exec_main_cmd(self, chat_id, ctx, wait_msg, api_func):
        """메인방 명령어 실행을 위한 공통 래퍼"""
        if not ctx["is_main"]:
            stock_api.send_telegram(chat_id, f"⚠️ 메인 채팅방({DEFAULT_CHAT_ID}) 전용입니다.")
            return
        stock_api.send_telegram(chat_id, wait_msg)
        msg = api_func()
        stock_api.send_telegram(chat_id, msg,keyboard=COMMON_BUTTON)

    # ===============================================================
    # 🎮 [Handlers] 리팩토링된 명령어 핸들러
    # ===============================================================
    def cmd_help(self, chat_id, args, ctx):
        if ctx["is_main"]:
            msg = (
                "🔭 <b>[바티인베스트 메인 본부]</b>\n════════════\n"
                "<b>1. /전광판</b> : 시장 지수 및 섹터 랭킹\n"
                "<b>2. /랭킹</b> : 급등/급락 종목 Top 5\n"
                "<b>3. /섹터비교 A B</b> : 수익률 대결\n════════════"
            )
        elif ctx["is_industry"]:
            msg = (
                f"🏭 <b>[{ctx['industry_name']} 산업방 매뉴얼]</b>\n════════════\n"
                "<b>1. /업황</b> : 섹터 등락 및 주도주\n"
                "<b>2. /자금</b> : 수급 흐름\n"
                "<b>3. /테마</b> : 전체 테마 리스트\n"
                "<b>4. /테마 [이름]</b> : 상세 분석\n"
                "<b>5. /시총순</b> : 전체 종목 시총순 랭킹\n"
                "<b>6. /실적</b> : 전체 종목 실적 랭킹\n════════════"
            )
        else:
            msg = (
                "💼 <b>[채팅방 매뉴얼]</b>\n════════════\n"
                "<b>1. /주가</b> : 시세 팩트 체크\n"
                "<b>2. /브리핑</b> : 투자포인트 & 일정\n"
                "<b>3. /재무</b> : 매출/영업이익 추이\n"
                "<b>4. /수급</b> : 외인/기관 매매 동향\n"
                "<b>5. /진단</b> : 차트 기술적 분석\n"
                "<b>6. /제보 [내용]</b>\n"
                "   (일정: /제보 삼성전자 3/20 주주총회)\n"
                "   (투자포인트: /제보 현대차 역대 최고 실적 달성)\n════════════"
            )
        stock_api.send_telegram(chat_id, msg)

    # --- 1. 산업방 전용 (리팩토링) ---
    def cmd_sector_status(self, chat_id, args, ctx):
        self._exec_industry_cmd(chat_id, ctx, f"🔍 <b>[{ctx['industry_name']}]</b> 시황 분석 중...", stock_api.get_sector_status)

    def cmd_sector_funds(self, chat_id, args, ctx):
        self._exec_industry_cmd(chat_id, ctx, f"💰 <b>[{ctx['industry_name']}]</b> 자금 흐름 집계 중...", stock_api.get_sector_funds)

    def cmd_industry_cap_ranking(self, chat_id, args, ctx):
        self._exec_industry_cmd(chat_id, ctx, f"📊 <b>[{ctx['industry_name']}]</b> 줄 세우는 중...", stock_api.get_industry_cap_ranking)

    def cmd_industry_financial_ranking(self, chat_id, args, ctx):
        self._exec_industry_cmd(chat_id, ctx, f"📊 <b>[{ctx['industry_name']}]</b> 재무 분석 중...", stock_api.get_industry_financial_ranking)

    def cmd_theme_analysis(self, chat_id, args, ctx):
        if not ctx["is_industry"]:
            stock_api.send_telegram(chat_id, "⚠️ <b>산업별 채팅방</b> 전용 명령어입니다.")
            return
        
        if not args:
            stock_api.send_telegram(chat_id, f"🔍 <b>[{ctx['industry_name']}]</b> 전체 테마 분석 중...")
            msg = stock_api.get_industry_theme_ranking(ctx["industry_name"])
        else:
            msg = stock_api.get_theme_analysis(args[0])
        stock_api.send_telegram(chat_id, msg,keyboard=COMMON_BUTTON)

    def cmd_sector_comparison(self, chat_id, args, ctx):
        if not ctx["is_industry"]:
            stock_api.send_telegram(chat_id, "⚠️ <b>산업별 채팅방</b> 전용 명령어입니다.")
            return

        target_name = args[0] if args else ctx["industry_name"]
        msg_txt = f"🧩 <b>'{target_name}'</b> 분석 중..." if args else f"📊 <b>[{target_name}]</b> 전체 분석 중..."
        
        stock_api.send_telegram(chat_id, f"{msg_txt}\n(잠시만 기다려주세요)")
        msg = stock_api.get_sector_fundamental_comparison(target_name)
        stock_api.send_telegram(chat_id, msg,keyboard=COMMON_BUTTON)

    # --- 2. 메인방 전용 (리팩토링) ---
    def cmd_market_board(self, chat_id, args, ctx):
        self._exec_main_cmd(chat_id, ctx, "🔭 <b>시장 데이터 집계 중...</b>", stock_api.get_market_scoreboard)

    def cmd_ranking(self, chat_id, args, ctx):
        self._exec_main_cmd(chat_id, ctx, "🏆 <b>랭킹 산출 중...</b>", stock_api.get_universe_ranking)

    # --- 3. 공용 명령어 ---
    def cmd_compare_sectors(self, chat_id, args, ctx):
        if len(args) < 2:
            stock_api.send_telegram(chat_id, "⚠️ 사용법: /섹터비교 [섹터1] [섹터2]")
            return
        msg = stock_api.compare_sectors(args[0], args[1])
        stock_api.send_telegram(chat_id, msg,keyboard=COMMON_BUTTON)

    # --- 4. 종목 분석 (공통 실행기 사용) ---
    def _execute_stock_command(self, chat_id, args, ctx, api_func, fail_msg):
        code, name = self._resolve_target_stock(args, ctx["matched_key"])
        if not code:
            if name: stock_api.send_telegram(chat_id, f"❓ <b>'{name}'</b> 종목을 찾을 수 없습니다.")
            else: stock_api.send_telegram(chat_id, "⚠️ 종목명을 입력해주세요. (예: /주가 삼성전자)")
            return

        try:
            if api_func.__code__.co_argcount == 2:
                msg = api_func(code, name)
            else:
                msg = api_func(name)
        except:
            msg = api_func(name)
        
        if msg: stock_api.send_telegram(chat_id, msg,keyboard=COMMON_BUTTON)
        else: stock_api.send_telegram(chat_id, f"❌ <b>[{name}]</b> {fail_msg}")

    def cmd_stock_price(self, chat_id, args, ctx):
        self._execute_stock_command(chat_id, args, ctx, stock_api.get_stock_detail, "데이터 조회 실패")

    def cmd_stock_supply(self, chat_id, args, ctx):
        self._execute_stock_command(chat_id, args, ctx, stock_api.get_investor_trend_cumulative, "수급 데이터 없음")

    def cmd_stock_chart(self, chat_id, args, ctx):
        self._execute_stock_command(chat_id, args, ctx, stock_api.get_stock_chart, "차트 분석 실패")

    def cmd_stock_finance(self, chat_id, args, ctx):
        self._execute_stock_command(chat_id, args, ctx, stock_api.get_stock_fundamental, "재무 정보 없음")
    
    def cmd_stock_briefing(self, chat_id, args, ctx):
        self._execute_stock_command(chat_id, args, ctx, stock_api.get_stock_briefing, "브리핑 정보 없음")

    # ===============================================================
    # 🚀 [Main Dispatcher] 명령어 처리 진입점
    # ===============================================================
    def handle_command(self, chat_id, text, matched_key=None):
        parts = text.split()
        if not parts: return
        
        cmd = parts[0]
        args = parts[1:]

        handler = self.commands.get(cmd)
        if not handler: return

        ctx = self._get_chat_context(chat_id, matched_key)

        try:
            handler(chat_id, args, ctx)
        except Exception as e:
            logging.error(f"Command Execution Error ({cmd}): {e}")
            stock_api.send_telegram(chat_id, f"❌ 명령 처리 중 오류 발생: {e}")

    def _handle_pro_callback(self, cb_id: str, chat_id: int, message_id: int, parts: list):
        """PRO 채널 승인/거절 인라인 버튼 처리."""
        base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

        def answer(text='', alert=False):
            try:
                self.listener_session.post(
                    f"{base_url}/answerCallbackQuery",
                    json={'callback_query_id': cb_id, 'text': text, 'show_alert': alert},
                    timeout=5
                )
            except Exception:
                pass

        def edit(text):
            try:
                self.listener_session.post(
                    f"{base_url}/editMessageText",
                    json={'chat_id': chat_id, 'message_id': message_id,
                          'text': text, 'parse_mode': 'HTML'},
                    timeout=5
                )
            except Exception:
                pass

        if len(parts) < 3:
            answer("잘못된 요청", alert=True)
            return

        action = parts[1]   # approve / reject
        try:
            uid = int(parts[2])
        except (ValueError, IndexError):
            answer("잘못된 ID", alert=True)
            return

        if action == 'approve':
            months = int(parts[3]) if len(parts) > 3 else 1
            try:
                import pro_channel as _pro
                row = _pro.add_member(uid, months=months)
                paid_until = row.get('paid_until', '?')
                ok_invite  = _pro.send_invite(uid, months=months)
                invite_str = "✅ 초대 링크 발송 완료" if ok_invite else "⚠️ 초대 링크 발송 실패"
                answer(f"✅ {months}개월 승인 완료")
                edit(
                    f"✅ <b>[승인 완료]</b>\n\n"
                    f"텔레그램 ID: <code>{uid}</code>\n"
                    f"구독 기간: <b>{months}개월</b>\n"
                    f"만료일: <b>{paid_until}</b>\n"
                    f"{invite_str}"
                )
            except Exception as e:
                logging.error(f"[PRO approve] 오류: {e}")
                answer("❌ 오류 발생", alert=True)
                edit(f"❌ <b>[승인 오류]</b>\n<code>{e}</code>")

        elif action == 'reject':
            try:
                answer("❌ 거절 처리")
                edit(
                    f"❌ <b>[거절]</b>\n\n"
                    f"텔레그램 ID: <code>{uid}</code>\n"
                    f"구독 신청이 거절되었습니다."
                )
                # 신청자에게 거절 안내 DM
                try:
                    import bot_commands as _bc
                    _bc._reply(uid,
                        "안타깝게도 이번에는 구독 신청이 승인되지 않았습니다.\n"
                        "문의사항이 있으시면 @batiinvest로 연락해 주세요."
                    )
                except Exception:
                    pass
            except Exception as e:
                logging.error(f"[PRO reject] 오류: {e}")
                answer("오류 발생")

        else:
            answer("알 수 없는 액션", alert=True)

    def handle_callback(self, callback_q):
        cb_id = callback_q['id']
        data = callback_q['data']
        chat_id = callback_q['message']['chat']['id']
        message_id = callback_q['message']['message_id']

        parts = data.split('|')

        # PRO 채널 콜백 (승인/거절)
        if parts[0] == 'PRO':
            self._handle_pro_callback(cb_id, chat_id, message_id, parts)
            return

        if len(parts) != 3 or parts[0] != "REP": return

        action_type = parts[1]
        report_id = parts[2]

        if report_id not in self.pending_reports:
            stock_api.send_telegram(chat_id, "❌ 만료되었거나 이미 처리된 제보입니다.")
            return

        report = self.pending_reports.pop(report_id)
        
        if action_type == "DEL":
            edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"🗑 제보가 거절(삭제)되었습니다.\n(내용: {report['content']})",
                "parse_mode": "HTML"
            }
            self.listener_session.post(edit_url, json=payload)
            return

        tab_name = "일정" if action_type == "SCH" else "투자포인트"
        
        self.input_wait_list[chat_id] = {
            "tab": tab_name,
            "origin_msg_id": message_id,
            "report_data": report
        }
        
        display_tab_name = "일정" if tab_name == "일정" else "투자포인트"
        
        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": f"✏️ <b>[{display_tab_name}] 수정 등록 모드</b>\n\n구글 시트에 저장할 내용을 입력해주세요.\n(원문: <code>{report['content']}</code>)",
            "parse_mode": "HTML"
        }
        self.listener_session.post(edit_url, json=payload)

    def telegram_listener(self):
        logging.info("👂 바티대리 리스너 대기 중")
        
        allowed_configs = [
            str(x) for x in list(COMPANY_CHAT_IDS.values()) + 
            list(INDUSTRY_CHAT_IDS.values()) + 
            [DEFAULT_CHAT_ID, ADMIN_CHAT_ID]
        ]

        while True:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                params = {
                    "timeout": 10,
                    "offset": self.last_update_id + 1,
                    "allowed_updates": ["message", "callback_query", "channel_post", "edited_message"]
                }
                res = self.listener_session.post(url, json=params, timeout=30)
                
                if res.status_code == 200:
                    updates = res.json().get("result", [])
                    for update in updates:
                        self.last_update_id = update["update_id"]
                        
                        if "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                            continue

                        message = None
                        if "message" in update:
                            message = update["message"]
                        elif "channel_post" in update:
                            message = update["channel_post"]
                        else:
                            continue

                        chat_id = message.get("chat", {}).get("id")
                        user_name = message.get("from", {}).get("first_name", "Unknown")
                        text = message.get("text", "")
                        
                        if chat_id in self.input_wait_list:
                            self.process_admin_input(chat_id, text)
                            continue

                        if text.startswith("/제보"):
                            self.process_report(chat_id, message.get("message_id"), user_name, text)
                            continue

                        matched_key = None
                        str_cid = str(chat_id)
                        
                        if str_cid in allowed_configs: matched_key = str_cid
                        elif message.get("chat", {}).get("username"):
                            u_id = f"@{message['chat']['username']}"
                            if u_id in allowed_configs: matched_key = u_id
                            else:
                                for cfg in allowed_configs:
                                    if cfg.lower() == u_id.lower(): matched_key = cfg; break
                        
                        # ── 1:1 DM 처리 (구독 신청 / 봇 명령어) ──────────
                        if not matched_key:
                            if message.get("chat", {}).get("type") == "private":
                                try:
                                    import bot_commands as _bc
                                    _bc._handle(update)
                                except Exception as _bce:
                                    logging.debug(f"bot_commands handle 오류: {_bce}")
                            continue

                        words = text.split()
                        if not words:
                            continue
                        cmd_word = words[0]
                        if cmd_word in self.commands:
                            self.handle_command(chat_id, text, matched_key)
                            
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"Listener Error: {e}")
                time.sleep(5)

    def run(self):
        logging.info("🚀 바티대리 출근 완료")
        stock_api.send_telegram(DEFAULT_CHAT_ID, "🚀 <b>채팅방 기능 업데이트</b>(시스템 리팩토링 완료)")
        t = threading.Thread(target=self.monitoring_loop, daemon=True)
        t.start()
        self.telegram_listener()

if __name__ == "__main__":
    scanner = KisMyStockScanner()
    scanner.run()