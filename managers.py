# managers.py
import json
import os
import logging
import requests
import time
import threading
from typing import Optional, List, Dict
from collections import deque
# time 모듈(line 6)을 datetime.time으로 덮지 않도록 별칭 사용 — time.sleep() 호출부 보호
from datetime import datetime, time as dtime, timedelta, timezone

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
# 🚦 [Infrastructure] KIS 전역 호출량 제한기
#   스캐너(30초 주기 전 종목)·수집기·봇 조회가 동시에 돌면
#   KIS 개인앱 초당 한도(20건/s)를 넘겨 조용히 실패한다.
#   모든 KIS 호출 직전에 acquire()로 슬라이딩 윈도우 통과를 보장.
# ==========================================
class RateLimiter:
    """스레드 안전 슬라이딩 윈도우 리미터."""
    def __init__(self, max_calls: int = 15, per_seconds: float = 1.0):
        self.max_calls = max_calls
        self.per = per_seconds
        self._calls = deque()
        self._lock = threading.Lock()

    def acquire(self):
        """호출 슬롯 확보까지 블로킹."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.per:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.per - (now - self._calls[0])
            time.sleep(max(wait, 0.01))


# 한도 20/s 대비 보수적 15/s (토큰 발급 등 비계측 호출 여유분)
kis_rate_limiter = RateLimiter(max_calls=15, per_seconds=1.0)


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
                _note_kis_token_failure(str(data)[:150])
                return None
                
            # KIS 실제 응답 키는 access_token_token_expired ('token_token_expired'는 과거 오타
            # — 항상 12h fallback을 타서 토큰 재발급이 잦았음). 구키는 호환용으로 유지.
            expired_str = data.get('access_token_token_expired') or data.get('token_token_expired')
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
            _KIS_TOKEN_FAIL['streak'] = 0
            _KIS_TOKEN_FAIL['alerted'] = False
            return access_token

        except Exception as e:
            logging.error(f"❌ 토큰 요청 에러: {e}")
            _note_kis_token_failure(str(e)[:150])
            return None

    def kis_get(self, tr_id: str, path: str, params: Dict,
                custtype: str = None, timeout: int = 10) -> Optional[Dict]:
        """KIS GET 공통 실행기 — 토큰·헤더·레이트리미터·세션·JSON 파싱만 담당.
        params는 호출부가 전부 구성 (rt_cd 판정도 호출부 책임 — 경고 로그 문맥 보존).

        collect_market(수급·백필·투자의견)/collect_macro(지수)/collect_estimates/
        is_kr_holiday 가 각자 반복하던 token→headers→req.get 보일러플레이트 통합.
        rt_cd '0' 필터까지 포함한 상위 래퍼는 call_api().
        반환: 파싱된 JSON dict, 실패(토큰 없음·네트워크·비JSON) 시 None.
        """
        if not KIS_APP_KEY:
            return None
        token = self.get_token()
        if not token:
            return None
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id,
        }
        if custtype:
            headers["custtype"] = custtype
        try:
            kis_rate_limiter.acquire()
            res = global_session.get(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/{path}",
                headers=headers, params=params, timeout=timeout,
            )
            return res.json()
        except Exception as e:
            logging.error(f"KIS GET Error ({path}): {e}")
            return None

    # ✅ [Phase 2] stock_api.py에서 이관된 API 호출 로직
    def call_api(self, tr_id: str, path: str, code: str, extra_params: Dict = None, custtype: str = None, timeout: int = 10) -> Optional[Dict]:
        """종목 단건 조회용 상위 래퍼 — 기본 FID 파라미터 + rt_cd '0' 필터 포함"""
        if not KIS_APP_KEY or not code: return None

        clean_code = code.split('.')[0]
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": clean_code}
        if extra_params:
            params.update(extra_params)

        data = self.kis_get(tr_id, path, params, custtype, timeout)
        if not data:
            return None
        if data.get('rt_cd') != '0':
            logging.debug(f"KIS rt_cd!=0 ({path}, {clean_code}): {data.get('msg_cd')} {data.get('msg1')}")
            return None
        return data

# ==========================================
# 🤖 [Manager] 텔레그램 봇 관리자
# ==========================================
# ── 텔레그램 발송 영구실패 집계 (400/403/401 — 재시도 무의미) ────────────────
# 구: 로그만 남아 채널 하나가 통째로 끊겨도(강퇴·chat not found) 관리자가 몰랐다.
# 여기서 직접 알림을 보내지 않는다 — 발송 장애 중 알림 발송은 재귀. 19:50 운영요약이 읽어간다.
_SEND_FAILURES: dict = {}


def note_send_failure(chat_id, reason: str):
    rec = _SEND_FAILURES.setdefault(str(chat_id), {'count': 0, 'reason': ''})
    rec['count'] += 1
    rec['reason'] = str(reason)[:100]


def pop_send_failures() -> dict:
    """집계 반환 후 비움 (운영요약이 하루 1회 호출)."""
    snap = dict(_SEND_FAILURES)
    _SEND_FAILURES.clear()
    return snap


# ── KIS 토큰 발급 연속 실패 알림 ─────────────────────────────────────────────
# 토큰이 안 나오면 시세감시·수집 전체가 멈추는데 기존엔 logging.error 한 줄뿐이었다.
_KIS_TOKEN_FAIL = {'streak': 0, 'alerted': False}


def _note_kis_token_failure(detail: str):
    _KIS_TOKEN_FAIL['streak'] += 1
    if _KIS_TOKEN_FAIL['streak'] >= 3 and not _KIS_TOKEN_FAIL['alerted']:
        _KIS_TOKEN_FAIL['alerted'] = True
        try:
            from telegram_utils import get_admin_chat_id
            admin = get_admin_chat_id()
            if admin:
                telegram_bot.send_message(
                    admin,
                    "🚨 <b>[KIS]</b> 토큰 발급 3회 연속 실패 — 시세·수집 전면 중단 위험\n"
                    f"└ {detail}")
        except Exception:
            logging.exception("⚠️ [KIS] 토큰 실패 알림 발송 실패")


class TelegramBotManager:
    MAX_LEN = 4000   # 텔레그램 한도 4096 — HTML 태그 여유분 감안한 보수적 분할 기준

    def __init__(self):
        self.token = TELEGRAM_BOT_TOKEN

    @staticmethod
    def _split_text(text: str, limit: int = 4000) -> list:
        """4096자 초과 메시지를 줄 경계에서 분할 (태그 절단 최소화)."""
        if len(text) <= limit:
            return [text]
        chunks, cur = [], ""
        for line in text.split("\n"):
            while len(line) > limit:          # 한 줄 자체가 한도 초과 → 강제 절단
                chunks.append(line[:limit])
                line = line[limit:]
            if len(cur) + len(line) + 1 > limit:
                chunks.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)
        return chunks

    def send_message(self, chat_id: str, text: str, preview: bool = False, keyboard: Dict = None) -> bool:
        """모든 청크 발송 성공 시 True. DRY_RUN 환경변수 설정 시 발송 없이 True."""
        if not self.token or not chat_id or not text:
            return False
        if os.getenv("DRY_RUN"):
            logging.info(f"[DRY-RUN] → {chat_id}: {text[:100]!r} ({len(text)}자)")
            return True

        chunks = self._split_text(text, self.MAX_LEN)
        if len(chunks) > 1:
            logging.info(f"✂️ 텔레그램 장문 분할: {len(text)}자 → {len(chunks)}건 ({chat_id})")
        all_ok = True
        for i, chunk in enumerate(chunks):
            # inline 키보드는 마지막 청크에만 부착
            kb = keyboard if i == len(chunks) - 1 else None
            all_ok = self._send_one(chat_id, chunk, preview, kb) and all_ok
        return all_ok

    def _send_one(self, chat_id: str, text: str, preview: bool, keyboard: Dict) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        if keyboard:
            payload["reply_markup"] = keyboard

        for attempt in range(3):
            try:
                res = global_session.post(url, json=payload, timeout=10)
            except Exception as e:
                # global_session이 연결 계층 재시도를 이미 수행한 뒤의 실패
                logging.error(f"⚠️ 텔레그램 연결 에러 ({chat_id}): {e}")
                note_send_failure(chat_id, f"연결 에러: {e}")
                return False

            if res.status_code == 200:
                return True

            if res.status_code == 429:
                retry_after = 3
                try:
                    retry_after = int(res.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    pass
                logging.warning(f"⚠️ 텔레그램 429 ({chat_id}) — {retry_after}초 대기 ({attempt + 1}/3)")
                time.sleep(min(retry_after, 60))
                continue

            if res.status_code == 400 and payload.get("parse_mode") and "parse" in res.text.lower():
                # parse_mode를 None으로 두면 JSON에 parse_mode:null로 직렬화돼
                # 텔레그램이 "unsupported parse_mode"(400)로 거부 → 키 자체를 제거해 평문 발송.
                logging.warning(f"⚠️ HTML 파싱 에러 → 평문 재전송 ({chat_id}): {res.text[:150]}")
                payload.pop("parse_mode", None)
                continue

            if res.status_code in (400, 403):
                # chat not found / bot kicked / user blocked 등 — 재시도 무의미
                logging.error(f"❌ 텔레그램 영구 실패 ({chat_id}, {res.status_code}): {res.text[:200]}")
                note_send_failure(chat_id, f"{res.status_code} {res.text[:80]}")
                return False

            if res.status_code == 401:
                logging.critical("🚨 텔레그램 토큰 인증 실패(401) — TELEGRAM_BOT_TOKEN 확인 필요")
                note_send_failure(chat_id, "401 봇 토큰 인증 실패")
                return False

            logging.error(f"⚠️ 텔레그램 전송 실패 ({chat_id}, {res.status_code}): {res.text[:200]}")
            note_send_failure(chat_id, f"{res.status_code} {res.text[:80]}")
            return False

        logging.error(f"❌ 텔레그램 발송 포기 ({chat_id}) — 재시도 초과")
        note_send_failure(chat_id, "재시도 초과 (429/타임아웃)")
        return False

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
        self.OPEN_TIME = dtime(8, 50)
        self.CLOSE_TIME = dtime(15, 30)

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
        # KIS API 조회 (kis_get 공통 실행기 — 구현 시절엔 rate limiter 미적용이었음)
        try:
            data = kis_auth.kis_get(
                'CTCA0903R', 'quotations/chk-holiday',
                {'BASS_DT': today_str, 'CTX_AREA_FK': '', 'CTX_AREA_NK': ''},
                custtype='P', timeout=5,
            )
            if data and data.get('rt_cd') == '0':
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
            '20260717',                                    # 제헌절 (2026년부터 공휴일 재지정)
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
        """현재 장이 열려있는지 확인 (평일 & 08:50 ~ 15:30 & 공휴일 제외)"""
        now = self.get_now()
        
        # 1. 주말 체크
        if now.weekday() >= 5: 
            return False
            
        # 2. 시간 체크
        current_time = now.time()
        if not (self.OPEN_TIME <= current_time <= self.CLOSE_TIME):
            return False

        # 3. 공휴일 체크 — 휴장일에도 KIS 시세 API는 직전 거래일 데이터를
        #    반환하므로 여기서 막지 않으면 스테일 알림이 나감 (일자별 캐시, API 하루 1회)
        return not self.is_kr_holiday()

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