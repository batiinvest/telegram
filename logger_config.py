"""
logger_config.py — 공통 로깅 설정 모듈
─────────────────────────────────────────
전 스크립트에서 일관된 로그 포맷·레벨·핸들러를 사용하기 위한 중앙 설정.

사용법:
    from logger_config import get_logger
    log = get_logger(__name__)
    log.info("작업 시작")

run_all.py (장기 실행 데몬) 전용 파일 핸들러:
    from logger_config import setup_root_logger
    setup_root_logger()   ← 최상단에서 1회만 호출
"""

import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, quote_plus

# ── 공통 포맷 ─────────────────────────────────────────────────────────────────
LOG_FORMAT      = '%(asctime)s [%(levelname)s] %(name)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
LOG_LEVEL       = logging.INFO

# ── 로그 파일 설정 (run_all.py 데몬 전용) ────────────────────────────────────
LOG_DIR      = os.path.join(os.path.dirname(__file__), 'logs')
LOG_FILE     = os.path.join(LOG_DIR, 'bati.log')
LOG_MAX_BYTES   = 10 * 1024 * 1024   # 10 MB
LOG_BACKUP_COUNT = 5


# ── 키 가리기 (로그 · job_runs · 관리자 방 알림 · notice_history 공용) ─────────
# requests 오류 문구에는 요청 URL이 그대로 들어간다 — 'Max retries exceeded with url:
# /api/list.json?crtfc_key=…'. 이걸 str(e)로 저장·발송하면 DART 키가 job_runs.error와
# 관리자 방에 그대로 남았다(2026-11-25 21:00 job_collect_financials).
# 여기 패턴은 텔레그램 본문(HTML)에도 쓰므로 값만 가리고 태그·따옴표·괄호는 남긴다.
_MASK = '***'
_VAL = r"[^\s&'\"<>()\[\]{},;|\\]+"     # 쿼리 값: 다음 &·공백·따옴표·괄호·태그 앞까지
# 이 프로젝트가 실제로 쓰는 이름: DART crtfc_key · KIS appkey/appsecret · 공공데이터 serviceKey ·
# Supabase apikey · 네이버 X-Naver-Client-Secret · Gemini x-goog-api-key (+ 흔한 이름)
_KEY_NAMES = (r"crtfc_key|app_?key|app_?secret|service_?key|api_?key|approval_?key|auth_?key|"
              r"secret_?key|access_?token|refresh_?token|client_?secret|"
              r"x-naver-client-secret|x-goog-api-key")
_GENERIC_NAMES = r"token|secret|password|passwd|pwd"

_SECRET_PATTERNS = [
    # crtfc_key=… · appkey: … (이름이 키 그 자체인 것 — = 또는 :)
    (re.compile(rf"(?i)(?<![A-Za-z0-9])({_KEY_NAMES})(\s*[=:]\s*){_VAL}"), rf"\1\2{_MASK}"),
    # token=… · password=… (흔한 이름은 = 일 때만 — 'Secret: …' 같은 뉴스 제목은 건드리지 않는다)
    (re.compile(rf"(?i)(?<![A-Za-z0-9])({_GENERIC_NAMES})={_VAL}"), rf"\1={_MASK}"),
    # 'appkey': '…' · "Authorization": "…" (dict·JSON·헤더 repr)
    (re.compile(rf"(?i)(['\"])({_KEY_NAMES}|{_GENERIC_NAMES}|authorization)\1(\s*:\s*)(['\"])[^'\"\n]*\4"),
     rf"\1\2\1\3\4{_MASK}\4"),
    # Bearer 토큰
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/]{16,}=*"), rf"\1{_MASK}"),
    # JWT (Supabase 키·KIS 접근 토큰)
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"), '***JWT***'),
    # 텔레그램 봇 토큰 — URL의 /bot<id>:<token>/ 포함
    (re.compile(r"(\d{6,12}):[A-Za-z0-9_-]{30,}"), rf"\1:{_MASK}"),
    # Google API 키 (Gemini)
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), f"AIza{_MASK}"),
    # Supabase 새 형식 키
    (re.compile(r"\bsb_(secret|publishable)_[A-Za-z0-9_\-]{8,}"), rf"sb_\1_{_MASK}"),
]

# 환경변수에 든 비밀값 자체 — 이름 없이 값만 찍혀도(예: 라이브러리가 키를 따로 보여 줄 때) 가린다.
# .env는 config import 때 늦게 읽히므로 처음 부를 때·환경변수 수가 바뀔 때·5분마다 다시 모은다.
_SECRET_ENV_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|_PW|_HASH|SESSION)(_\d+)?$")
_SECRET_LITERALS = {'n': -1, 'at': 0.0, 'vals': ()}


def _secret_literals() -> tuple:
    c = _SECRET_LITERALS
    n, now = len(os.environ), time.monotonic()
    if n != c['n'] or now - c['at'] > 300:
        vals = set()
        for k, v in list(os.environ.items()):
            if v and len(v) >= 8 and _SECRET_ENV_NAME.search(k.upper()):
                vals.update({v, quote(v, safe=''), quote_plus(v)})   # URL에 인코딩돼 실린 꼴도
        c.update(n=n, at=now, vals=tuple(sorted(vals, key=len, reverse=True)))
    return c['vals']


def mask_secrets(text) -> str:
    """문자열에서 API 키·토큰 값을 '***'로 가린다. 이름(crtfc_key= 등)과 나머지 문구는 남긴다.
    잘라서 저장할 곳은 **가린 뒤에** 자른다 — 먼저 자르면 키 앞부분이 이름 없이 남을 수 있다."""
    if text is None:
        return ''
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    for v in _secret_literals():
        if v in s:
            s = s.replace(v, _MASK)
    for pattern, repl in _SECRET_PATTERNS:
        s = pattern.sub(repl, s)
    return s


# ── 로그 전용 추가 마스킹 (예전 규칙 — 넓게 잡아도 되는 로그에만) ──────────────
_SENSITIVE_PATTERNS = [
    # API key / secret (쿼리스트링 또는 dict key)
    (re.compile(r'(api[_-]?key|secret|token|password|passwd|Authorization)\s*[=:]\s*\S+',
                re.IGNORECASE), r'\1=***'),
    # KIS appkey / appsecret
    (re.compile(r'(appkey|appsecret|APP_KEY|APP_SECRET)\s*[=:]\s*\S+',
                re.IGNORECASE), r'\1=***'),
]


def _mask_log(text: str) -> str:
    s = mask_secrets(text)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        s = pattern.sub(replacement, s)
    return s


_EXC_FORMATTER = logging.Formatter()


class SensitiveDataFilter(logging.Filter):
    """로그에서 민감 정보(키·토큰·시크릿·패스워드)를 마스킹.

    핸들러에 붙인다 — 로거에 붙인 필터는 하위 로거(logging.getLogger(__name__))에서
    올라온 기록에는 걸리지 않는다. 본문뿐 아니라 traceback(exc_info=True)도 가린다:
    traceback 마지막 줄에 예외 문구(요청 URL·키)가 그대로 찍히기 때문이다."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = _mask_log(str(record.getMessage()))
            # args를 비워서 getMessage()가 이미 마스킹된 문자열을 쓰도록 교체
            record.msg  = msg
            record.args = None
            if record.exc_info and not record.exc_text:
                # Formatter.format은 exc_text가 있으면 그것을 쓴다 — 미리 만들어 가려 둔다
                record.exc_text = _EXC_FORMATTER.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = _mask_log(record.exc_text)
            if record.stack_info:
                record.stack_info = _mask_log(record.stack_info)
        except Exception:
            pass
        return True


# ── 외부 라이브러리 로그 억제 ────────────────────────────────────────────────
_NOISY_LOGGERS = [
    'httpx', 'httpcore', 'urllib3', 'charset_normalizer',
    'openai', 'anthropic', 'supabase', 'postgrest',
    'realtime', 'storage3', 'gotrue',
]


def _suppress_noisy_loggers(level: int = logging.WARNING):
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(level)


# ── 핸들러 빌더 ───────────────────────────────────────────────────────────────
def _make_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def _make_console_handler() -> logging.StreamHandler:
    h = logging.StreamHandler()
    h.setFormatter(_make_formatter())
    h.addFilter(SensitiveDataFilter())
    return h


def _make_file_handler(path: str = LOG_FILE) -> RotatingFileHandler:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES,
                            backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
    h.setFormatter(_make_formatter())
    h.addFilter(SensitiveDataFilter())
    return h


# ── 공개 API ──────────────────────────────────────────────────────────────────
def get_logger(name: str) -> logging.Logger:
    """
    표준 설정이 적용된 Logger 반환.
    basicConfig를 직접 호출하지 않아도 됩니다.

    사용법:
        log = get_logger(__name__)
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(LOG_LEVEL)
        logger.addHandler(_make_console_handler())
        logger.propagate = True
    return logger


def setup_root_logger(log_file: str = LOG_FILE):
    """
    루트 로거 전역 설정 — run_all.py 최상단에서 1회만 호출.
    콘솔 + 파일(RotatingFileHandler) 동시 출력.
    """
    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()

    root.setLevel(LOG_LEVEL)
    root.addHandler(_make_console_handler())
    root.addHandler(_make_file_handler(log_file))

    _suppress_noisy_loggers()
    logging.info(f'[LogConfig] 로거 초기화 완료 → {log_file}')
