# news_main.py — 뉴스 봇 (개선 버전)
#
# 개선 사항:
#   1. 스팸/광고성 뉴스 패턴 필터링 (매수 1위, 급등 예고 등)
#   2. 중복 감지 강화 (제목 정규화 + 핵심 키워드 기반)
#   3. 종목명 단순 언급 vs 실질 보도 구분
#   4. 출처별 신뢰도 가중치 (증권사 리포트 광고 필터)
#   5. 시간 윈도우 기반 중복 제거 (같은 이벤트 반복 발송 방지)

import datetime
import time
import logging
import html
import urllib.parse
import re
import hashlib

from email.utils import parsedate_to_datetime
from typing import Dict
from collections import deque

from managers import market_timer, HistoryManager, get_session
import stock_api

from config import (
    NAVER_KEYS, DEFAULT_CHAT_ID, COMPANY_KEYWORDS,
    COMPANY_CODES, INDUSTRY_CHAT_IDS, INDUSTRY_HIERARCHY, COMMON_BUTTON,
    COMPANY_TO_INDUSTRY
)

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except Exception:
    _BRIDGE_OK = False


# 중복 판정 유사도 임계 (어절+문자 bigram 자카드). app_config 'news_dup_similarity'로 조정 가능.
DUP_SIM_THRESHOLD = 0.45
# 이벤트키 중복은 제목이 이 정도는 겹쳐야 인정 — 순수 키워드 충돌(예: ETF'상장' vs ADR'상장') 오탐 방지. 고정.
DUP_EVENT_GATE = 0.30

_KST = datetime.timezone(datetime.timedelta(hours=9))


def _load_news_filters():
    """
    app_config에서 스팸패턴/실질보도 키워드 로드.
    DB에 없는 기본값은 최초 1회 시드 후 로드.
    """
    global SPAM_PATTERNS, _SPAM_RE, MEANINGFUL_KEYWORDS, LOW_TRUST_SOURCES, DUP_SIM_THRESHOLD
    if not _BRIDGE_OK:
        return
    _bridge.seed_defaults({
        "news_spam_patterns":       "\n".join(SPAM_PATTERNS),
        "news_meaningful_keywords": ",".join(MEANINGFUL_KEYWORDS),
        "news_low_trust_sources":   ",".join(LOW_TRUST_SOURCES),
        "news_dup_similarity":      str(DUP_SIM_THRESHOLD),
    })
    try:
        client = _bridge._get_client()
        if not client:
            return
        res = client.table('app_config').select('key,value').in_(
            'key', ['news_spam_patterns', 'news_meaningful_keywords',
                    'news_low_trust_sources', 'news_dup_similarity']
        ).execute()
        cfg = {r['key']: r['value'] for r in (res.data or [])}

        if cfg.get('news_spam_patterns'):
            patterns = [p.strip() for p in cfg['news_spam_patterns'].split('\n') if p.strip()]
            if patterns:
                SPAM_PATTERNS = patterns
                _SPAM_RE = [re.compile(p, re.IGNORECASE) for p in SPAM_PATTERNS]
                logging.info(f"✅ [뉴스봇] 스팸패턴 {len(SPAM_PATTERNS)}개 DB에서 로드")

        if cfg.get('news_meaningful_keywords'):
            kws = [k.strip() for k in cfg['news_meaningful_keywords'].split(',') if k.strip()]
            if kws:
                MEANINGFUL_KEYWORDS = kws
                logging.info(f"✅ [뉴스봇] 실질보도 키워드 {len(MEANINGFUL_KEYWORDS)}개 DB에서 로드")

        if cfg.get('news_low_trust_sources'):
            sources = [s.strip() for s in cfg['news_low_trust_sources'].split(',') if s.strip()]
            if sources:
                LOW_TRUST_SOURCES = sources
                logging.info(f"✅ [뉴스봇] 신뢰도 낮은 출처 {len(LOW_TRUST_SOURCES)}개 DB에서 로드")

        if cfg.get('news_dup_similarity'):
            try:
                v = float(cfg['news_dup_similarity'])
                if 0 < v <= 1:
                    DUP_SIM_THRESHOLD = v
                    logging.info(f"✅ [뉴스봇] 중복 유사도 임계 {DUP_SIM_THRESHOLD} DB에서 로드")
            except (TypeError, ValueError):
                pass
    except Exception as e:
        logging.warning(f"⚠️ [뉴스봇] 필터 키워드 DB 로드 실패 (기본값 사용): {e}")


# reload_flag 소비는 watchdog 단일 창구 — 재로드 시 뉴스 필터도 함께 갱신되도록 콜백 등록.
# (구: 봇 자체 check_reload_flag 폴링 — bridge 싱글톤 플래그 경쟁 소비 문제)
try:
    from config import on_reload as _on_reload
    _on_reload(_load_news_filters)
except Exception:
    pass


# ══════════════════════════════════════════════════════
#  스팸/광고성 뉴스 필터 패턴
#  — 제목에 이 패턴이 포함되면 발송하지 않음
# ══════════════════════════════════════════════════════
SPAM_PATTERNS = [
    # 주식 광고/추천 패턴
    r'매수\s*[0-9]?\s*위', r'매도\s*[0-9]?\s*위',
    r'순매수\s*[0-9]?\s*위', r'순매도\s*[0-9]?\s*위',
    r'거래량\s*[0-9]?\s*위', r'거래대금\s*[0-9]?\s*위',
    r'상승률\s*[0-9]?\s*위', r'하락률\s*[0-9]?\s*위',
    r'급등\s*(예고|포착|신호|임박|예상)',
    r'(오늘|내일|이번주).{0,10}(급등|폭등|대박)',
    r'(포착|발굴|추천).{0,10}(종목|주식)',
    r'[0-9]+%\s*(급등|폭등)\s*(예고|전망|예상)',
    r'(목표주가|목표가).{0,5}(상향|하향)',  # 단순 목표가 변경 (리포트 요약 광고)
    r'증권사.{0,10}(추천|매수)',
    r'(HTS|MTS|앱).{0,10}(이벤트|할인|혜택)',
    # 단순 순위/통계성 뉴스
    r'(코스피|코스닥).{0,5}(상승|하락)\s*(종목|주)',
    r'52주\s*(신고가|신저가)\s*종목',
    r'(외국인|기관).{0,5}(순매수|순매도)\s*[0-9]?\s*위',
    r'(개인|외인).{0,10}(매집|탈출)',
    # 클릭베이트
    r'지금\s*당장',
    r'(놓치면|모르면)\s*(후회|손해)',
    r'[0-9]+배\s*(수익|상승)\s*(비결|전략|방법)',
]

# 컴파일된 패턴 (성능)
_SPAM_RE = [re.compile(p, re.IGNORECASE) for p in SPAM_PATTERNS]

# ══════════════════════════════════════════════════════
#  신뢰 낮은 출처 키워드 (URL/언론사명에 포함 시 가중치 하락)
# ══════════════════════════════════════════════════════
from telegram_utils import LOW_TRUST_SOURCES   # 단일 정의 — telegram_utils.py

# ══════════════════════════════════════════════════════
#  실질 보도 판단 키워드
#  — 이 중 하나라도 제목/본문에 있어야 발송
#  — 없으면 단순 언급으로 판단해 스킵
# ══════════════════════════════════════════════════════
MEANINGFUL_KEYWORDS = [
    # 공시/실적
    '공급계약', '수주', '납품', '계약', '협약', 'MOU',
    '실적', '매출', '영업이익', '순이익', '적자', '흑자',
    '잠정', '실적발표', '가이던스',
    # 기업 이벤트
    '인수', '합병', 'M&A', '분할', '지분', '투자유치',
    '증자', '감자', '자사주', '배당',
    '상장', '상폐', '관리종목', '거래정지',
    '임원', '대표이사', '사외이사', '횡령', '배임',
    # 제품/기술
    '출시', '개발', '특허', '인허가', '승인', 'FDA', '식약처',
    '임상', '상용화', '양산',
    # 시장/거시
    '규제', '법안', '정책', '관세', '제재',
    '파업', '리콜', '화재', '사고',
    # 주가 관련 (실질적)
    '공매도', '블록딜', '오버행', '워런트',
]


# ── 무음 실패 관리자 알림 (공시봇 main._note_api_failure 패턴) ────────────────
# 구: 키 전량 소진·401 무효화로 종일 0건이어도 스레드는 살아 있어
#     워치독·운영요약 어디에도 안 잡혔다.
_ADMIN_ALERTED: set = set()


def _alert_admin_once(tag: str, msg: str):
    key = (datetime.date.today().isoformat(), tag)
    if key in _ADMIN_ALERTED:
        return
    _ADMIN_ALERTED.add(key)
    try:
        from telegram_utils import get_admin_chat_id
        admin = get_admin_chat_id()
        if admin:
            stock_api.send_telegram(admin, msg)
    except Exception:
        logging.exception("⚠️ [뉴스] 관리자 알림 발송 실패")


# ══════════════════════════════════════════════════════════════════
#  제목 유사도 유틸 — 모듈 레벨(뉴스봇 + 저녁요약 공용 단일 출처)
#  daily_summary.py가 동일 기준으로 이벤트 군집화를 하려면 필요하다.
#  로직 변경 시 양쪽에 함께 반영되도록 여기서만 정의한다.
# ══════════════════════════════════════════════════════════════════

def normalize_title(title: str) -> str:
    """
    중복 감지를 위한 제목 정규화.
    - 숫자/단위 제거 (주가 변동 숫자가 달라도 같은 기사)
    - 언론사 접두/접미 제거
    - 공백 정규화
    """
    t = html.unescape(title).lower()
    t = re.sub(r'<[^>]+>', '', t)                    # HTML 태그 제거
    t = re.sub(r'\[.*?\]|\(.*?\)', '', t)            # 괄호 내용 제거 (언론사명 등)
    t = re.sub(r'[0-9,]+(?:\.[0-9]+)?%?', '', t)    # 숫자/퍼센트 제거
    t = re.sub(r'[^\w가-힣]', ' ', t)                # 특수문자 → 공백
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def title_sig(norm_title: str, company: str) -> set:
    """
    유사도용 시그니처 — 종목명을 제외한 어절 + 각 어절의 문자 2-gram 집합.
    SequenceMatcher(문자열 정렬)보다 한국어 어순 뒤바뀜·조사 차이에 강하다.
    """
    words = [w for w in norm_title.split() if w and w != company.lower()]
    s = set(words)
    for w in words:
        for i in range(len(w) - 1):
            s.add(w[i:i + 2])
    return s


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def extract_event_key(company: str, title: str) -> str:
    """
    제목에서 핵심 이벤트 키워드를 추출해 이벤트 키 생성.
    - 공백 제거 후 매칭 → '공급 계약' = '공급계약'
    - 키워드 길이 내림차순 매칭 → '공급계약'이 '계약'보다 우선
    - 제목만 사용(본문은 부수 단어로 엉뚱한 키를 유발)
    예: "삼성전자 반도체 공급계약" → "삼성전자_공급계약"
    """
    compact = re.sub(r'\s+', '', title)
    for kw in sorted(MEANINGFUL_KEYWORDS, key=len, reverse=True):
        if re.sub(r'\s+', '', kw) in compact:
            return f"{company}_{kw}"
    return ""


class NaverNewsBot:
    def __init__(self):
        self.base_url = "https://openapi.naver.com/v1/search/news.json"
        self.history  = HistoryManager("sent_news.txt", max_len=3000)

        # 중복 감지용 메모리 — 종목별 분리. {종목명: [(제목해시, 정규화제목, 발송시각, 시그니처집합)]}
        # (구: 전 종목 공통 dict + 최근 200개 슬라이스 → 발송량 많은 종목 이력이 희석됐다)
        self._title_cache: Dict[str, list] = {}
        self._title_cache_ttl = datetime.timedelta(hours=24)  # 24시간 내 동일 제목 중복

        # 이벤트 기반 중복: {종목명+이벤트키: (발송시각, 시그니처집합)}
        # 시그니처를 함께 보관 — 같은 이벤트 키라도 제목이 최소한 겹칠 때만 중복 처리(순수 키워드 충돌 방지)
        self._event_cache: Dict[str, tuple] = {}
        self._event_cache_ttl = datetime.timedelta(hours=6)  # 같은 이벤트 6시간 내 재발송 방지

        self._send_retry: Dict[str, int] = {}  # link → 전채널 발송 실패 재시도 횟수

        self.session     = get_session()
        self.key_index   = 0
        self.api_keys    = NAVER_KEYS  # config에서 미설정 슬롯 필터링됨 — 빈 리스트 가능
        self.current_key = self.api_keys[0] if self.api_keys else {"id": "", "secret": ""}
        self.consecutive_429 = 0
        self._auth_fail_streak = 0   # 401/403 연속 — 전 키 소진 시 관리자 알림
        self._update_session_headers()

        KST = datetime.timezone(datetime.timedelta(hours=9))
        self.START_TIME = datetime.datetime.now(KST)
        self._loop_count = 0

        # DB에서 필터 키워드 로드 (코드 기본값 덮어쓰기)
        _load_news_filters()

        # 봇 재시작에도 중복 캐시가 유지되도록 최근 24h 발송이력을 notice_history에서 복원
        self._rehydrate_cache()

    def _update_session_headers(self):
        self.session.headers.update({
            "X-Naver-Client-Id":     self.current_key["id"],
            "X-Naver-Client-Secret": self.current_key["secret"]
        })

    def _rotate_key(self):
        self.key_index   = (self.key_index + 1) % len(self.api_keys)
        self.current_key = self.api_keys[self.key_index]
        self._update_session_headers()
        logging.warning(f"🔄 Key Rotated to #{self.key_index + 1}")

    def _wait_for_quota_reset(self):
        now        = datetime.datetime.now()
        tomorrow   = now + datetime.timedelta(days=1)
        reset_time = datetime.datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 5, 0)
        seconds_until_reset = (reset_time - now).total_seconds()

        stock_api.send_telegram(DEFAULT_CHAT_ID,
            "⛔ <b>[뉴스 봇 일시정지]</b>\nAPI 한도 소진. 내일 0시 5분 재가동.")
        _alert_admin_once('news_quota',
            f"⛔ <b>[뉴스봇]</b> 네이버 API 한도 전 키 소진 — "
            f"{reset_time.strftime('%m-%d %H:%M')}까지 수집 정지")
        while seconds_until_reset > 0:
            time.sleep(min(600, seconds_until_reset))
            seconds_until_reset -= 600
        self.consecutive_429 = 0
        stock_api.send_telegram(DEFAULT_CHAT_ID, "🌅 <b>[뉴스 봇 재가동]</b>")

    # ──────────────────────────────────────────
    #  1. 스팸 필터
    # ──────────────────────────────────────────
    def is_spam(self, title: str, link: str) -> bool:
        """광고성/순위성/클릭베이트 뉴스 필터"""
        for pattern in _SPAM_RE:
            if pattern.search(title):
                logging.debug(f"🚫 스팸 필터: {title}")
                return True
        return False

    # ──────────────────────────────────────────
    #  2. 실질 보도 판단
    # ──────────────────────────────────────────
    def is_meaningful(self, title: str, desc: str) -> bool:
        """단순 언급이 아닌 실질적 보도인지 판단"""
        text = title + " " + desc
        return any(kw in text for kw in MEANINGFUL_KEYWORDS)

    # ──────────────────────────────────────────
    #  3. 제목 정규화 (중복 감지용)
    # ──────────────────────────────────────────
    # 아래 4종은 모듈 레벨 함수에 위임 — 저녁요약(daily_summary)과 동일 기준 보장.
    def _normalize_title(self, title: str) -> str:
        return normalize_title(title)

    def _title_hash(self, title: str) -> str:
        return hashlib.md5(normalize_title(title).encode()).hexdigest()

    def _sig(self, norm_title: str, company: str) -> set:
        return title_sig(norm_title, company)

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        return jaccard(a, b)

    # ──────────────────────────────────────────
    #  4. 이벤트 키 추출 (같은 이벤트 반복 방지)
    # ──────────────────────────────────────────
    def _extract_event_key(self, company: str, title: str) -> str:
        return extract_event_key(company, title)

    # ──────────────────────────────────────────
    #  5. 통합 중복 감지
    # ──────────────────────────────────────────
    def is_duplicate(self, title: str, desc: str, company: str, link: str) -> bool:
        now = datetime.datetime.now()

        # 종목별 캐시 정리 (TTL) — 해당 종목 이력만 훑는다(전 종목 200개 슬라이스 폐기)
        clist = [e for e in self._title_cache.get(company, [])
                 if now - e[2] < self._title_cache_ttl]
        self._title_cache[company] = clist
        self._event_cache = {
            k: v for k, v in self._event_cache.items()
            if now - v[0] < self._event_cache_ttl
        }

        norm_new = self._normalize_title(title)
        t_hash   = self._title_hash(title)
        sig_new  = self._sig(norm_new, company)

        # (a) 정확한 URL 중복 → history에서 처리
        # (b) 제목 해시 중복 (숫자/단위 달라도 같은 기사) — 같은 종목 내
        if any(e[0] == t_hash for e in clist):
            logging.debug(f"🔍 제목 해시 중복: {title}")
            return True

        # (c) 제목 유사도 중복 — 같은 종목 24h 이력과 자카드 비교
        if norm_new and len(norm_new.split()) >= 4:   # 짧은 제목은 유사도 오탐 방지(정확 해시만)
            for e in clist:
                if e[3] and self._jaccard(sig_new, e[3]) >= DUP_SIM_THRESHOLD:
                    logging.debug(f"🔍 제목 유사도 중복(≥{DUP_SIM_THRESHOLD}): {title}")
                    return True

        # (d) 이벤트 키 중복 (같은 종목 + 같은 이벤트 타입 6시간 내)
        #     단, 제목이 최소한 겹칠 때만(DUP_EVENT_GATE) — 순수 키워드 충돌 오탐 방지
        event_key = self._extract_event_key(company, title)
        if event_key and event_key in self._event_cache:
            _ts, sig_old = self._event_cache[event_key]
            if self._jaccard(sig_new, sig_old) >= DUP_EVENT_GATE:
                logging.debug(f"🔍 이벤트 중복 ({self._event_cache_ttl}): {event_key}")
                return True

        return False

    def _persist_news(self, company: str, title: str, desc: str,
                      link: str, pub_dt) -> None:
        """
        발송한 뉴스를 daily_news에 적재 — 저녁 요약이 읽는 유일한 뉴스 소스.

        네이버 재조회로는 하루치를 복원할 수 없다(봇이 종일 폴링해 키 대부분이
        일일 한도 소진). 즉 '보낼 때 남기지 않으면 그날 뉴스는 영영 사라진다'.
        다만 적재 실패가 발송·중복관리에 영향을 주면 안 되므로 완전히 격리한다.
        """
        try:
            from db_client import get_supabase_client
            sb = get_supabase_client()
            if not sb:
                return
            base_date = (pub_dt.astimezone(_KST).date() if pub_dt
                         else datetime.datetime.now(_KST).date()).isoformat()
            try:
                source = urllib.parse.urlparse(link).netloc.replace('www.', '') or None
            except Exception:
                source = None
            sb.table('daily_news').upsert({
                'base_date':    base_date,
                'corp_name':    company,
                'stock_code':   COMPANY_CODES.get(company),
                'industry':     COMPANY_TO_INDUSTRY.get(company),
                'title':        title,
                'description':  (desc or '')[:1000] or None,
                'link':         link,
                'source':       source,
                'published_at': pub_dt.isoformat() if pub_dt else None,
            }, on_conflict='base_date,corp_name,link',
               ignore_duplicates=True).execute()
        except Exception as e:
            logging.debug(f"[뉴스적재] 실패(무시): {e}")

    def _register_sent(self, title: str, desc: str, company: str):
        """발송 후 종목별 캐시에 등록"""
        now     = datetime.datetime.now()
        norm_t  = self._normalize_title(title)
        t_hash  = self._title_hash(title)
        sig     = self._sig(norm_t, company)
        self._title_cache.setdefault(company, []).append((t_hash, norm_t, now, sig))

        event_key = self._extract_event_key(company, title)
        if event_key:
            self._event_cache[event_key] = (now, sig)

    def _rehydrate_cache(self):
        """
        봇 재시작 시 중복 캐시 소실 방지 — 최근 24h '뉴스' 발송이력을
        notice_history에서 읽어 종목별 제목/이벤트 캐시를 복원한다.
        (구: 순수 메모리 dict라 재시작마다 초기화 → 같은 기사 재발송이 최다 원인)
        """
        if not _BRIDGE_OK:
            return
        try:
            client = _bridge._get_client()
            if not client:
                return
            since = (datetime.datetime.now(datetime.timezone.utc)
                     - self._title_cache_ttl).isoformat()
            res = (client.table('notice_history')
                   .select('target,content,created_at')
                   .like('content', '[뉴스] %')
                   .gte('created_at', since)
                   .order('created_at', desc=False)   # 시간순 — 이벤트 캐시에 최신이 남도록
                   .execute())
            n = 0
            for row in (res.data or []):
                company = row.get('target') or ''
                content = row.get('content') or ''
                if content.startswith('[뉴스]'):
                    content = content[len('[뉴스]'):].strip()
                title = content
                if not company or not title:
                    continue
                try:
                    # DB는 UTC — 서버(KST) 나이브 now()와 맞추기 위해 로컬 나이브로 변환
                    ts = datetime.datetime.fromisoformat(
                        row['created_at']).astimezone().replace(tzinfo=None)
                except Exception:
                    continue
                norm_t = self._normalize_title(title)
                sig    = self._sig(norm_t, company)
                self._title_cache.setdefault(company, []).append(
                    (self._title_hash(title), norm_t, ts, sig))
                ek = self._extract_event_key(company, title)
                if ek:
                    self._event_cache[ek] = (ts, sig)
                n += 1
            logging.info(f"♻️ [뉴스봇] 중복 캐시 복원: {n}건 "
                         f"({len(self._title_cache)}개 종목)")
        except Exception as e:
            logging.warning(f"⚠️ [뉴스봇] 캐시 복원 실패 (빈 캐시로 시작): {e}")

    # ──────────────────────────────────────────
    #  6. 뉴스 검색
    # ──────────────────────────────────────────
    def search_news(self, query: str, display: int = 5) -> list:
        encoded = urllib.parse.quote(query)
        url     = f"{self.base_url}?query={encoded}&display={display}&sort=date"
        try:
            res = self.session.get(url, timeout=3)
            if res.status_code == 200:
                self.consecutive_429 = 0
                self._auth_fail_streak = 0
                return res.json().get('items', [])
            elif res.status_code == 429:
                self.consecutive_429 += 1
                if self.consecutive_429 >= len(self.api_keys):
                    self._wait_for_quota_reset()
                else:
                    self._rotate_key()
                    time.sleep(0.5)
                return []
            else:
                # 401(키 무효)·403 등이 무음이면 수집 전체가 조용히 0건이 됨 — 로그 + 로테이션
                logging.warning(
                    f"⚠️ [뉴스] API {res.status_code} (key#{self.key_index + 1}): {res.text[:100]}"
                )
                if res.status_code in (401, 403):
                    self._auth_fail_streak += 1
                    if self._auth_fail_streak >= max(len(self.api_keys), 1):
                        _alert_admin_once('news_auth',
                            f"🚨 <b>[뉴스봇]</b> 네이버 API 인증 실패({res.status_code})가 "
                            f"전 키({len(self.api_keys)}개)에 연속 발생 — 뉴스 수집 중단 상태\n"
                            f"└ {res.text[:120]}")
                    self._rotate_key()
                    time.sleep(0.5)
                return []
        except Exception:
            return []

    def clean_text(self, text: str) -> str:
        return html.unescape(text).replace("<b>", "").replace("</b>", "").replace("&quot;", '"')

    def is_new_news(self, pub_date_str: str) -> bool:
        try:
            return parsedate_to_datetime(pub_date_str) >= (self.START_TIME - datetime.timedelta(minutes=10))
        except Exception:
            return False

    # ──────────────────────────────────────────
    #  7. 메인 루프
    # ──────────────────────────────────────────
    def run(self):
        if not self.api_keys:
            # 키 없이 돌면 401 무한 루프 + 워치독 재시작 반복 → 대기 모드로 고정
            logging.critical("⛔ 네이버 API 키 없음 (NAVER_ID_1~10 미설정) — 뉴스봇 대기 모드")
            _alert_admin_once('news_nokey',
                "🚨 <b>[뉴스봇]</b> 네이버 API 키 없음(NAVER_ID_1~10 미설정) — 대기 모드로 고정됨")
            while True:
                time.sleep(3600)
        logging.info("🚀 News Bot Started with Optimized Filtering")

        while True:
            is_active = market_timer.is_market_open()
            cycle_sleep = 3 if is_active else 60

            self._loop_count += 1
            if self._loop_count % 200 == 1 and _BRIDGE_OK:
                try:
                    _bridge.heartbeat("news_bot")
                except Exception:
                    pass
                # reload_flag 자체 폴링 제거 — watchdog이 단일 소비자,
                # 필터 갱신은 config.on_reload(_load_news_filters) 콜백으로 수신

            for company_info in COMPANY_KEYWORDS:
                company_name = company_info["name"]
                news_items   = self.search_news(company_name)

                for item in reversed(news_items):
                    link = item.get('originallink') or item.get('link', '')

                    # ① URL 중복
                    if self.history.contains(link):
                        continue

                    # ② 발행 시각 필터 (너무 오래된 뉴스)
                    if not self.is_new_news(item['pubDate']):
                        continue

                    title = self.clean_text(item.get('title', ''))
                    desc  = self.clean_text(item.get('description', ''))

                    # ③ 종목명이 제목에 포함되어야 함
                    if company_name not in title:
                        continue

                    # ④ 스팸/광고성 뉴스 필터
                    if self.is_spam(title, link):
                        self.history.add(link)
                        continue

                    # ⑤ 실질 보도 판단 (의미 있는 키워드 없으면 스킵)
                    if not self.is_meaningful(title, desc):
                        logging.debug(f"⏭ 실질 보도 아님: {title}")
                        self.history.add(link)
                        continue

                    # ⑥ 중복 감지
                    if self.is_duplicate(title, desc, company_name, link):
                        self.history.add(link)
                        continue

                    # ── 발송 ──
                    time_str, pub_dt = "", None
                    try:
                        pub_dt = parsedate_to_datetime(item['pubDate'])
                        time_str = pub_dt.strftime("%H:%M")
                    except Exception:
                        pass

                    stock_msg = ""
                    if company_name in COMPANY_CODES:
                        price_info = stock_api.get_stock_price(COMPANY_CODES[company_name])
                        if price_info:
                            stock_msg = f"{price_info}\n"

                    hidden_link = f"<a href='{link}'>&#8203;</a>"
                    msg = (
                        f"{hidden_link}"
                        f"🏢 <b>[{company_name}] ({time_str})</b>\n{stock_msg}"
                        f"<b><a href='{link}'>{title}</a></b>\n\n"
                        f"📄 {desc}..."
                    )

                    industry = COMPANY_TO_INDUSTRY.get(company_name)
                    _results = []
                    if industry and industry in INDUSTRY_CHAT_IDS:
                        _results.append(stock_api.send_telegram(
                            INDUSTRY_CHAT_IDS[industry], msg,
                            preview=True, keyboard=COMMON_BUTTON
                        ))
                    # stock_api.get_company_chat_id()로 통일
                    # — 종목명 변경 시 stock_code fallback 포함
                    _cid = stock_api.get_company_chat_id(company_name)
                    if _cid:
                        _results.append(stock_api.send_telegram(
                            _cid, msg,
                            preview=True, keyboard=COMMON_BUTTON
                        ))

                    # 전 채널 발송 실패 → 링크 미기록으로 다음 사이클 재시도 (최대 2회)
                    if _results and not any(_results):
                        _n = self._send_retry.get(link, 0) + 1
                        self._send_retry[link] = _n
                        if _n < 2:
                            logging.warning(f"⚠️ [뉴스] 전 채널 발송 실패 — 재시도 예정: {title}")
                            continue
                        logging.error(f"❌ [뉴스] 발송 2회 실패 — 포기: {title}")
                    self._send_retry.pop(link, None)

                    if _BRIDGE_OK:
                        try:
                            _bridge.log_notice(
                                target=company_name,
                                content=f"[뉴스] {title}",
                                sent_count=len(_results),
                                ok_count=sum(1 for r in _results if r),
                            )
                        except Exception:
                            pass

                    self.history.add(link)
                    self._register_sent(title, desc, company_name)
                    self._persist_news(company_name, title, desc, link, pub_dt)
                    logging.info(f"✅ Sent: {company_name} - {title}")

                time.sleep(0.1)

            time.sleep(cycle_sleep)


if __name__ == "__main__":
    bot = NaverNewsBot()
    bot.run()
