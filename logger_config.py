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
from logging.handlers import RotatingFileHandler

# ── 공통 포맷 ─────────────────────────────────────────────────────────────────
LOG_FORMAT      = '%(asctime)s [%(levelname)s] %(name)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
LOG_LEVEL       = logging.INFO

# ── 로그 파일 설정 (run_all.py 데몬 전용) ────────────────────────────────────
LOG_DIR      = os.path.join(os.path.dirname(__file__), 'logs')
LOG_FILE     = os.path.join(LOG_DIR, 'bati.log')
LOG_MAX_BYTES   = 10 * 1024 * 1024   # 10 MB
LOG_BACKUP_COUNT = 5


# ── 민감 정보 마스킹 필터 ─────────────────────────────────────────────────────
_SENSITIVE_PATTERNS = [
    # Bearer token
    (re.compile(r'(Bearer\s+)[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE), r'\1***'),
    # API key / secret (쿼리스트링 또는 dict key)
    (re.compile(r'(api[_-]?key|secret|token|password|passwd|Authorization)\s*[=:]\s*\S+',
                re.IGNORECASE), r'\1=***'),
    # KIS appkey / appsecret
    (re.compile(r'(appkey|appsecret|APP_KEY|APP_SECRET)\s*[=:]\s*\S+',
                re.IGNORECASE), r'\1=***'),
    # JWT 토큰 형식 (xxx.yyy.zzz)
    (re.compile(r'eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+'), '***JWT***'),
    # 텔레그램 봇 토큰 (/bot<id>:<token>)
    (re.compile(r'(bot)(\d{6,}):[A-Za-z0-9_-]{30,}', re.IGNORECASE), r'\1\2:***'),
]


class SensitiveDataFilter(logging.Filter):
    """로그 메시지에서 민감 정보(토큰·시크릿·패스워드)를 마스킹."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
            for pattern, replacement in _SENSITIVE_PATTERNS:
                msg = pattern.sub(replacement, msg)
            # args를 비워서 getMessage()가 이미 마스킹된 문자열을 쓰도록 교체
            record.msg  = msg
            record.args = None
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
