# managers.py
import json
import os
import logging
import requests
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import deque # ✅ deque 임포트 확인 (없으면 추가)
from datetime import datetime, time, timedelta, timezone

# ✅ [추가 1] 스레드 풀 라이브러리 임포트
from concurrent.futures import ThreadPoolExecutor

# HTTP 어댑터 (재시도 로직)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 파일 잠금 라이브러리 (Safe Import)
try:
    from filelock import FileLock
except ImportError:
    from contextlib import contextmanager
    @contextmanager
    def FileLock(file_name, timeout=None): yield

# 구글 시트 라이브러리 (Safe Import)
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    logging.warning("⚠️ gspread 또는 google-auth가 설치되지 않았습니다.")

# 환경변수 로드
try:
    from config import (
        KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL,
        GOOGLE_JSON_FILE, GOOGLE_SHEET_URL,
        TELEGRAM_BOT_TOKEN # ✅ [Phase 2] 추가
    )
except ImportError:
    # config가 없을 경우를 대비한 방어 코드
    logging.error("❌ config.py를 찾을 수 없습니다.")
    KIS_APP_KEY = None
    KIS_APP_SECRET = None
    KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
    TELEGRAM_BOT_TOKEN = None

# 상수 설정
TOKEN_FILE = "kis_token.dat"

# ==========================================
# 🔢 [Util] 공통 타입 변환 헬퍼
#   stock_api.py / collect_market.py 중복 제거용 통합 버전
#   zero_as_none=True : 0값을 None으로 반환 (DB 저장 시 빈 값 구분)
# ==========================================
from typing import Optional as _Opt

def safe_float(val, zero_as_none: bool = False) -> _Opt[float]:
    """문자열·숫자를 float으로 안전하게 변환. 실패 시 None(zero_as_none=False면 0.0)."""
    try:
        v = str(val).replace(",", "").strip()
        if not v or v == "-":
            return None
        result = float(v)
        if zero_as_none and result == 0.0:
            return None
        return result
    except Exception:
        return None if zero_as_none else 0.0

def safe_int(val, zero_as_none: bool = False) -> _Opt[int]:
    """문자열·숫자를 int로 안전하게 변환. 실패 시 None(zero_as_none=False면 0)."""
    try:
        v = str(val).replace(",", "").strip()
        if not v or v == "-":
            return None
        result = int(float(v))
        if zero_as_none and result == 0:
            return None
        return result
    except Exception:
        return None if zero_as_none else 0

# ==========================================
# ⚙️ [Infrastructure] 세션 생성기
# ==========================================
def get_session():
    """재시도 로직이 포함된 requests 세션 생성"""
    session = requests.Session()
    retries = Retry(
        total=5, 
        backoff_factor=1, 
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('https://', adapter)
    session.headers.update({"content-type": "application/json; charset=utf-8"})
    return session

# 전역 세션 객체
global_session = get_session()

# ==========================================
# 💾 [Manager] 히스토리(중복 방지) 관리자 (✅ 신규 추가)
# ==========================================
class HistoryManager:
    """
    파일 기반의 전송 기록 관리자
    - 역할: 실행 시 파일 로드, 메모리 캐싱, 파일 자동 정리(최신 N개 유지), 실시간 저장
    - 사용처: DART 공시 봇, 네이버 뉴스 봇
    """
    def __init__(self, filename: str, max_len: int = 2000):
        self.filename = filename
        self.max_len = max_len
        self.history = set()
        self._load()

    def _load(self):
        """파일 로드 및 정리 (Clean-up)"""
        if not os.path.exists(self.filename): 
            return
        
        try:
            # 1. 효율적으로 마지막 max_len 줄만 읽기
            with open(self.filename, "r", encoding="utf-8") as f:
                lines = deque(f, maxlen=self.max_len)
            
            # 2. 메모리 캐시 (Set) 업데이트
            self.history = set(line.strip() for line in lines if line.strip())
            
            # 3. 정리된 내용으로 파일 덮어쓰기 (용량 관리)
            with open(self.filename, "w", encoding="utf-8") as f:
                f.writelines(lines)
                
            logging.info(f"🧹 [{self.filename}] 히스토리 로드 및 정리 완료 ({len(self.history)}개 유지)")
            
        except Exception as e:
            logging.error(f"❌ 히스토리 로드 실패 ({self.filename}): {e}")

    def contains(self, key: str) -> bool:
        """중복 여부 확인"""
        return key in self.history

    def add(self, key: str):
        """기록 추가 및 파일 저장"""
        if key in self.history: return
        
        self.history.add(key)
        try:
            with open(self.filename, "a", encoding="utf-8") as f:
                f.write(f"{key}\n")
        except Exception as e:
            logging.error(f"❌ 히스토리 저장 실패 ({self.filename}): {e}")

# ==========================================
# ⚙️ [Infrastructure] 실행 관리자 (✅ 신규 추가)
# ==========================================
class ExecutionManager:
    """전역 스레드 풀 관리자 (스레드 폭발 및 리소스 고갈 방지)"""
    def __init__(self, max_workers: int = 20):
        # thread_name_prefix를 지정하여 디버깅 용이성 확보
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="GlobalWorker")

    def submit_task(self, func, *args, **kwargs):
        """작업 제출 (Non-blocking)"""
        return self.executor.submit(func, *args, **kwargs)

    def shutdown(self):
        """스레드 풀 종료"""
        self.executor.shutdown(wait=True)

# ==========================================
# 🔐 [Manager] 토큰 및 API 관리자 (KIS)
# ==========================================
class KisAuthManager:
    def __init__(self):
        self.cached_token = None
        self.token_expiry = None
        self.lock_path = TOKEN_FILE + ".lock"

    def get_token(self) -> Optional[str]:
        # 1. 메모리 캐시 확인
        if self.cached_token and self.token_expiry:
            if datetime.now() < self.token_expiry - timedelta(minutes=5):
                return self.cached_token

        # 2. 파일 확인 (FileLock 적용)
        if os.path.exists(TOKEN_FILE):
            try:
                with FileLock(self.lock_path, timeout=30):
                    with open(TOKEN_FILE, 'r') as f:
                        data = json.load(f)
                        expire_dt = datetime.strptime(data['expired'], "%Y-%m-%d %H:%M:%S")
                        
                        if datetime.now() < expire_dt - timedelta(minutes=10):
                            self.cached_token = data['access_token']
                            self.token_expiry = expire_dt
                            logging.info("✅ 토큰 로드 완료 (파일)")
                            return self.cached_token
            except Exception as e:
                logging.warning(f"⚠️ 토큰 파일 읽기 실패 또는 락 타임아웃: {e}")

        # 3. API 신규 발급
        return self._issue_new_token()

    def _issue_new_token(self) -> Optional[str]:
        url = f"{KIS_BASE_URL}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET
        }
        try:
            res = global_session.post(url, data=json.dumps(body), timeout=10)
            data = res.json()
            access_token = data.get('access_token')
            
            if not access_token:
                logging.error(f"❌ 토큰 발급 실패: {data}")
                return None
                
            expired_str = data.get('token_token_expired')
            if not expired_str:
                expired_dt = datetime.now() + timedelta(hours=12)
            else:
                expired_dt = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S")

            try:
                with FileLock(self.lock_path, timeout=30):
                    with open(TOKEN_FILE, 'w') as f:
                        json.dump({
                            "access_token": access_token, 
                            "expired": expired_dt.strftime("%Y-%m-%d %H:%M:%S")
                        }, f)
            except Exception as e:
                logging.error(f"⚠️ 토큰 파일 저장 실패 (락 에러): {e}")
            
            self.cached_token = access_token
            self.token_expiry = expired_dt
            logging.info("✅ 새 토큰 발급 및 저장 완료")
            return access_token

        except Exception as e:
            logging.error(f"❌ 토큰 요청 에러: {e}")
            return None

    # ✅ [Phase 2] stock_api.py에서 이관된 API 호출 로직
    def call_api(self, tr_id: str, path: str, code: str, extra_params: Dict = None, custtype: str = None, timeout: int = 10) -> Optional[Dict]:
        """KIS API 호출을 전담하는 메서드"""
        if not KIS_APP_KEY or not code: return None
        
        token = self.get_token()
        if not token: return None

        clean_code = code.split('.')[0]
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/{path}"
        
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id
        }
        if custtype:
            headers["custtype"] = custtype

        # 기본 파라미터
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": clean_code}
        if extra_params:
            params.update(extra_params)

        try:
            res = global_session.get(url, headers=headers, params=params, timeout=timeout)
            data = res.json()
            
            if data['rt_cd'] != '0':
                return None
            return data
        except Exception as e:
            logging.error(f"API Call Error ({path}): {e}")
            return None

# ==========================================
# 🤖 [Manager] 텔레그램 봇 관리자
# ==========================================
class TelegramBotManager:
    def __init__(self):
        self.token = TELEGRAM_BOT_TOKEN

    # ✅ [Phase 2] stock_api.py에서 이관된 전송 로직
    def send_message(self, chat_id: str, text: str, preview: bool = False, keyboard: Dict = None):
        if not self.token: return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        
        payload = {
            "chat_id": chat_id, 
            "text": text, 
            "parse_mode": "HTML", 
            "disable_web_page_preview": not preview 
        }

        if keyboard:
            payload["reply_markup"] = keyboard
        
        try:
            res = global_session.post(url, json=payload, timeout=10)
            
            # 속도 제한 재시도
            if res.status_code == 429:
                logging.warning("⚠️ 텔레그램 속도 제한 (429). 3초 대기 후 재시도.")
                time.sleep(3)
                res = global_session.post(url, json=payload, timeout=10)

            # HTML 파싱 에러 시 텍스트 전송
            if res.status_code == 400 and "parse" in res.text.lower():
                logging.warning(f"⚠️ HTML 파싱 에러 발생. 일반 텍스트로 전환 전송. ({res.text})")
                payload["parse_mode"] = None 
                global_session.post(url, json=payload, timeout=10)
                
            elif res.status_code != 200:
                logging.error(f"⚠️ 텔레그램 전송 실패: {res.text}")

        except Exception as e:
            logging.error(f"⚠️ 텔레그램 연결 에러: {e}")

# ==========================================
# 📊 [Manager] 구글 시트 관리자
# ==========================================
class GoogleSheetManager:
    def __init__(self):
        self.client = None
        self.doc = None
        self.last_auth_time = None
        self.cache = {}
        self.cache_expiry = {}

    def get_doc(self):
        now = datetime.now()
        # 50분 경과 시 재연결
        if not self.client or not self.last_auth_time or (now - self.last_auth_time).seconds > 3000:
            if not os.path.exists(GOOGLE_JSON_FILE):
                logging.error("❌ 구글 키 파일 없음")
                return None
            try:
                scopes = [
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"
                ]
                creds = Credentials.from_service_account_file(GOOGLE_JSON_FILE, scopes=scopes)
                self.client = gspread.authorize(creds)
                self.doc = self.client.open_by_url(GOOGLE_SHEET_URL)
                self.last_auth_time = now
                logging.info("✅ 구글 시트 연결 갱신")
            except Exception as e:
                logging.error(f"❌ 구글 연결 실패: {e}")
                return None
        return self.doc

    def get_worksheet_data(self, sheet_name: str, ttl_seconds=300) -> List[Dict]:
        now = datetime.now()
        if sheet_name in self.cache and sheet_name in self.cache_expiry:
            if now < self.cache_expiry[sheet_name]:
                return self.cache[sheet_name]
        
        doc = self.get_doc()
        if not doc: return []
        
        try:
            worksheet = doc.worksheet(sheet_name)
            data = worksheet.get_all_records()
            self.cache[sheet_name] = data
            self.cache_expiry[sheet_name] = now + timedelta(seconds=ttl_seconds)
            logging.info(f"🌐 구글 시트 '{sheet_name}' 데이터 갱신 (API 호출)")
            return data
        except Exception as e:
            logging.error(f"Sheet Fetch Error ({sheet_name}): {e}")
            return self.cache.get(sheet_name, [])

    def clear_cache(self, sheet_name: str = None):
        if sheet_name:
            if sheet_name in self.cache: del self.cache[sheet_name]
            if sheet_name in self.cache_expiry: del self.cache_expiry[sheet_name]
        else:
            self.cache.clear()
            self.cache_expiry.clear()

# ==========================================
# ⏰ [Manager] 마켓 시간 관리자 (✅ 신규 추가)
# ==========================================
class MarketTimeManager:
    """
    한국 주식 시장(KST) 시간 및 휴장일 관리
    - 역할: 장 운영 시간 체크, 평일/주말 체크, KST 현재 시간 반환
    """
    def __init__(self):
        # KST 타임존 설정 (UTC+9)
        self.KST = timezone(timedelta(hours=9))
        
        # 장 운영 시간 설정 (08:50 ~ 15:30)
        self.OPEN_TIME = time(8, 50)
        self.CLOSE_TIME = time(15, 30)

    def get_now(self) -> datetime:
        """현재 KST 시간 반환"""
        return datetime.now(self.KST)

    def is_weekday(self) -> bool:
        """오늘이 평일(월~금)인지 확인"""
        return self.get_now().weekday() < 5

    def is_kr_holiday(self) -> bool:
        """오늘이 한국 휴장일(주말+공휴일)인지 확인
        KIS API CTCA0903R (국내휴장일조회)로 실제 개장일 여부 확인
        API 실패 시 주말 여부로 fallback
        """
        now = self.get_now()
        # 주말이면 바로 True
        if now.weekday() >= 5:
            return True
        # 공휴일 캐시 (당일 기준)
        today_str = now.strftime('%Y%m%d')
        if hasattr(self, '_holiday_cache') and self._holiday_cache.get('date') == today_str:
            return self._holiday_cache['is_holiday']
        # KIS API 조회
        try:
            import requests as _req
            # kis_auth 인스턴스에서 토큰 가져오기
            try:
                token = kis_auth.get_token()
            except Exception:
                token = None
            url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/chk-holiday"
            headers = {
                'authorization': f'Bearer {token}',
                'appkey':    KIS_APP_KEY,
                'appsecret': KIS_APP_SECRET,
                'tr_id':     'CTCA0903R',
                'custtype':  'P',
            }
            r = _req.get(url, headers=headers,
                        params={'BASS_DT': today_str, 'CTX_AREA_FK': '', 'CTX_AREA_NK': ''},
                        timeout=5)
            data = r.json()
            if data.get('rt_cd') == '0':
                rows = data.get('output', [])
                # 오늘 날짜 행 찾기
                for row in rows:
                    if row.get('bass_dt') == today_str:
                        opnd_yn = row.get('opnd_yn', '')
                        is_holiday = opnd_yn not in ('1', 'Y')  # '1' 또는 'Y' = 개장
                        self._holiday_cache = {'date': today_str, 'is_holiday': is_holiday}
                        logging.info(f"[휴장일] {today_str} 개장여부={opnd_yn} → {'휴장' if is_holiday else '개장'}")
                        return is_holiday
        except Exception as e:
            logging.warning(f"[휴장일] API 조회 실패 ({e}) → 정적 공휴일 목록으로 fallback")
        # fallback: 주말 + 정적 공휴일 목록
        KR_HOLIDAYS = {
            # 2025
            '20250101','20250128','20250129','20250130',  # 신정, 설날연휴
            '20250301',                                    # 3.1절
            '20250505','20250506',                         # 어린이날, 대체
            '20250602',                                    # 부처님오신날
            '20250606',                                    # 현충일
            '20250815',                                    # 광복절
            '20251003','20251005','20251006','20251007',   # 추석연휴
            '20251009',                                    # 한글날
            '20251225',                                    # 크리스마스
            # 2026
            '20260101',                                    # 신정
            '20260216','20260217','20260218',              # 설날연휴
            '20260302',                                    # 3.1절 대체
            '20260505',                                    # 어린이날
            '20260525',                                    # 부처님오신날 대체 (5/24 일요일)
            '20260606',                                    # 현충일
            '20260817',                                    # 광복절 대체 (8/15 토요일)
            '20260924','20260925','20260928',              # 추석연휴 (추정)
            '20261009',                                    # 한글날
            '20261225',                                    # 크리스마스
        }
        is_holiday = now.weekday() >= 5 or today_str in KR_HOLIDAYS
        self._holiday_cache = {'date': today_str, 'is_holiday': is_holiday}
        if today_str in KR_HOLIDAYS:
            logging.info(f"[휴장일] {today_str} 정적 공휴일 목록에서 휴장 확인")
        return is_holiday

    def is_us_holiday(self) -> bool:
        """오늘이 미국 NYSE 휴장일(주말+미국 공휴일)인지 확인 (KST 기준 날짜)
        글로벌 시황 브리핑 발송 여부 판단에 사용.
        미국 전날 장이 쉬었으면 → 브리핑할 데이터가 없으므로 발송 안 함.
        """
        now = self.get_now()
        if now.weekday() >= 5:
            return True
        # KST 기준 오늘 아침 06:30 브리핑 시, 미국 전날(현지 기준 어제) 장 확인
        # KST 06:30 = 미국 전날 (서머타임: EDT UTC-4 → 오후 5:30 / EST UTC-5 → 오후 4:30)
        # → KST 오늘 날짜 기준 weekday로 미국 전날 거래일 유추
        # 월요일(0) KST 아침 → 미국 기준 금요일이 마지막 거래일 (주말 체크는 위에서 처리)
        # 화~금(1~4) KST 아침 → 미국 기준 전날 평일
        # 즉 KST 월~금이면 미국도 전날 평일이므로 주말은 위에서 처리됨.
        # 핵심: 미국 공휴일만 추가로 체크
        today_str = now.strftime('%Y%m%d')
        cache_key = '_us_holiday_cache'
        if hasattr(self, cache_key) and getattr(self, cache_key, {}).get('date') == today_str:
            return getattr(self, cache_key)['is_holiday']

        US_HOLIDAYS = {
            # 2025 NYSE 휴장일
            '20250101',  # New Year's Day
            '20250120',  # MLK Day
            '20250217',  # Presidents' Day
            '20250418',  # Good Friday
            '20250526',  # Memorial Day
            '20250619',  # Juneteenth
            '20250704',  # Independence Day
            '20250901',  # Labor Day
            '20251127',  # Thanksgiving
            '20251225',  # Christmas
            # 2026 NYSE 휴장일
            '20260101',  # New Year's Day
            '20260119',  # MLK Day
            '20260216',  # Presidents' Day
            '20260403',  # Good Friday
            '20260525',  # Memorial Day
            '20260619',  # Juneteenth
            '20260703',  # Independence Day (Friday observe)
            '20260907',  # Labor Day
            '20261126',  # Thanksgiving
            '20261225',  # Christmas
        }
        # KST 기준 오늘(월~금)의 "미국 전날" 체크:
        # KST 월요일 아침 → 미국 금요일이 마지막 장. 금요일 날짜 = KST 오늘 - 3일
        # KST 화~금 아침 → 미국 전날 = KST 어제
        from datetime import timedelta as _td
        if now.weekday() == 0:  # KST 월요일 → 미국 금요일 체크
            us_prev_date = (now - _td(days=3)).strftime('%Y%m%d')
        else:
            us_prev_date = (now - _td(days=1)).strftime('%Y%m%d')

        is_holiday = us_prev_date in US_HOLIDAYS
        setattr(self, cache_key, {'date': today_str, 'is_holiday': is_holiday})
        if is_holiday:
            logging.info(f"[미국휴장] 미국 전날({us_prev_date}) 휴장 → 글로벌 시황 브리핑 생략")
        return is_holiday

    def is_market_open(self) -> bool:
        """현재 장이 열려있는지 확인 (평일 & 08:50 ~ 15:30)"""
        now = self.get_now()
        
        # 1. 주말 체크
        if now.weekday() >= 5: 
            return False
            
        # 2. 시간 체크
        current_time = now.time()
        return self.OPEN_TIME <= current_time <= self.CLOSE_TIME

    def seconds_until_open(self) -> float:
        """다음 개장까지 남은 초(seconds) 계산 (대기용)"""
        now = self.get_now()
        target = now.replace(hour=8, minute=50, second=0, microsecond=0)
        
        # 이미 지났으면 내일로
        if now > target:
            target += timedelta(days=1)
            
        # 내일이 주말이면 월요일로 (간단 구현)
        while target.weekday() >= 5:
            target += timedelta(days=1)
            
        return (target - now).total_seconds()

# ==========================================
# 🚀 인스턴스 생성 (싱글톤)
# ==========================================
kis_auth = KisAuthManager()
sheet_manager = GoogleSheetManager()
telegram_bot = TelegramBotManager() # ✅ 추가됨
execution_manager = ExecutionManager(max_workers=20)
market_timer = MarketTimeManager() # ✅ 추가됨