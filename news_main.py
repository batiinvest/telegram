# news_main.py — 뉴스 봇 (개선 버전)
#
# 개선 사항:
#   1. 스팸/광고성 뉴스 패턴 필터링 (매수 1위, 급등 예고 등)
#   2. 중복 감지 강화 (제목 정규화 + 핵심 키워드 기반)
#   3. 종목명 단순 언급 vs 실질 보도 구분
#   4. 출처별 신뢰도 가중치 (증권사 리포트 광고 필터)
#   5. 시간 윈도우 기반 중복 제거 (같은 이벤트 반복 발송 방지)

import requests
import datetime
import time
import logging
import os
import html
import urllib.parse
import re
import hashlib

from email.utils import parsedate_to_datetime
from typing import Set, Dict, List, Tuple
from collections import deque
from difflib import SequenceMatcher

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


def _load_news_filters():
    """
    app_config에서 스팸패턴/실질보도 키워드 로드.
    DB에 없는 기본값은 최초 1회 시드 후 로드.
    """
    global SPAM_PATTERNS, _SPAM_RE, MEANINGFUL_KEYWORDS, LOW_TRUST_SOURCES
    if not _BRIDGE_OK:
        return
    _bridge.seed_defaults({
        "news_spam_patterns":       "\n".join(SPAM_PATTERNS),
        "news_meaningful_keywords": ",".join(MEANINGFUL_KEYWORDS),
        "news_low_trust_sources":   ",".join(LOW_TRUST_SOURCES),
    })
    try:
        client = _bridge._get_client()
        if not client:
            return
        res = client.table('app_config').select('key,value').in_(
            'key', ['news_spam_patterns', 'news_meaningful_keywords', 'news_low_trust_sources']
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
    except Exception as e:
        logging.warning(f"⚠️ [뉴스봇] 필터 키워드 DB 로드 실패 (기본값 사용): {e}")


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


class NaverNewsBot:
    def __init__(self):
        self.base_url = "https://openapi.naver.com/v1/search/news.json"
        self.history  = HistoryManager("sent_news.txt", max_len=3000)

        # 중복 감지용 메모리: {정규화된_제목_해시: 발송시각}
        self._title_cache: Dict[str, tuple] = {}   # {hash: (정규화제목, 발송시각)}
        self._title_cache_ttl = datetime.timedelta(hours=24)  # 24시간 내 동일 제목 중복
        self._title_sim_threshold = 0.75  # 제목 유사도 중복 임계 (SequenceMatcher ratio)

        # 이벤트 기반 중복: {종목명+이벤트키: 발송시각}
        self._event_cache: Dict[str, datetime.datetime] = {}
        self._event_cache_ttl = datetime.timedelta(hours=6)  # 같은 이벤트 6시간 내 재발송 방지

        self._send_retry: Dict[str, int] = {}  # link → 전채널 발송 실패 재시도 횟수

        self.session     = get_session()
        self.key_index   = 0
        self.api_keys    = NAVER_KEYS  # config에서 미설정 슬롯 필터링됨 — 빈 리스트 가능
        self.current_key = self.api_keys[0] if self.api_keys else {"id": "", "secret": ""}
        self.consecutive_429 = 0
        self._update_session_headers()

        KST = datetime.timezone(datetime.timedelta(hours=9))
        self.START_TIME = datetime.datetime.now(KST)
        self._loop_count = 0

        # DB에서 필터 키워드 로드 (코드 기본값 덮어쓰기)
        _load_news_filters()

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
    def _normalize_title(self, title: str) -> str:
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

    def _title_hash(self, title: str) -> str:
        return hashlib.md5(self._normalize_title(title).encode()).hexdigest()

    # ──────────────────────────────────────────
    #  4. 이벤트 키 추출 (같은 이벤트 반복 방지)
    # ──────────────────────────────────────────
    def _extract_event_key(self, company: str, title: str) -> str:
        """
        제목에서 핵심 이벤트 키워드를 추출해 이벤트 키 생성.
        예: "삼성전자 반도체 공급계약" → "삼성전자_공급계약"
        """
        for kw in MEANINGFUL_KEYWORDS:
            if kw in title:
                return f"{company}_{kw}"
        return ""

    # ──────────────────────────────────────────
    #  5. 통합 중복 감지
    # ──────────────────────────────────────────
    def is_duplicate(self, title: str, desc: str, company: str, link: str) -> bool:
        now = datetime.datetime.now()

        # 캐시 정리 (메모리 절약)
        self._title_cache = {
            k: v for k, v in self._title_cache.items()
            if now - v[1] < self._title_cache_ttl
        }
        self._event_cache = {
            k: v for k, v in self._event_cache.items()
            if now - v < self._event_cache_ttl
        }

        # (a) 정확한 URL 중복 → history에서 처리
        # (b) 제목 해시 중복 (숫자/단위 달라도 같은 기사)
        t_hash = self._title_hash(title)
        if t_hash in self._title_cache:
            logging.debug(f"🔍 제목 해시 중복: {title}")
            return True

        # (c) 제목 유사도 중복 (정규화 제목 vs 최근 200개, SequenceMatcher ≥ 임계)
        norm_new = self._normalize_title(title)
        if norm_new and len(norm_new.split()) >= 4:   # 짧은 제목은 유사도 오탐 방지(정확 해시만)
            for _norm_cached, _ts in list(self._title_cache.values())[-200:]:
                if _norm_cached and SequenceMatcher(None, norm_new, _norm_cached).ratio() >= self._title_sim_threshold:
                    logging.debug(f"🔍 제목 유사도 중복(≥{self._title_sim_threshold}): {title}")
                    return True

        # (d) 이벤트 키 중복 (같은 종목 + 같은 이벤트 타입 6시간 내)
        event_key = self._extract_event_key(company, title + " " + desc)
        if event_key and event_key in self._event_cache:
            logging.debug(f"🔍 이벤트 중복 ({self._event_cache_ttl}): {event_key}")
            return True

        return False

    def _register_sent(self, title: str, desc: str, company: str):
        """발송 후 캐시에 등록"""
        now = datetime.datetime.now()
        t_hash = self._title_hash(title)
        self._title_cache[t_hash] = (self._normalize_title(title), now)

        event_key = self._extract_event_key(company, title + " " + desc)
        if event_key:
            self._event_cache[event_key] = now

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
                try:
                    if _bridge.check_reload_flag():
                        from config import reload_company_data
                        reload_company_data()
                        _load_news_filters()
                        logging.info("🔄 [뉴스봇] 종목/필터 데이터 재로드 완료")
                except Exception as _re:
                    logging.debug(f"reload_flag 체크 오류: {_re}")

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
                    time_str = ""
                    try:
                        time_str = parsedate_to_datetime(item['pubDate']).strftime("%H:%M")
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
                    logging.info(f"✅ Sent: {company_name} - {title}")

                time.sleep(0.1)

            time.sleep(cycle_sleep)


if __name__ == "__main__":
    bot = NaverNewsBot()
    bot.run()
