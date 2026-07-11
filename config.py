import json
import os
import logging
from dotenv import load_dotenv

# ✅ .env 파일 로드 (환경변수 활성화)
load_dotenv()

# ==========================================
# 1. 환경 설정 및 상수
# ==========================================

# 텔레그램 봇
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID = "@BatiInvestChat"

# DART API
DART_API_KEY = os.getenv("DART_API_KEY")

# 한국투자증권 (KIS)
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"

# AI 분석 (Gemini)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# ==========================================
# 10. 구글 스프레드 시트 설정 (일정 관리)
# ==========================================
GOOGLE_JSON_FILE = "google_secret.json"
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1fzZ95GevRno_fuq2zCNLtm9WHJUcaIQWeG_j9lvP1bQ/edit?gid=637679033#gid=637679033"
GOOGLE_SHEET_TAB = "시트1"

# ✅ 네이버 API 키 리스트
NAVER_KEYS = [
    {"id": os.getenv(f"NAVER_ID_{i}"), "secret": os.getenv(f"NAVER_SECRET_{i}")}
    for i in range(1, 11)
]
# 미설정 슬롯 제거 — 키가 10개 미만일 때 rotation이 None 키로 도는 것 방지
NAVER_KEYS = [k for k in NAVER_KEYS if k["id"] and k["secret"]]

if NAVER_KEYS:
    NAVER_CLIENT_ID = NAVER_KEYS[0]["id"]
    NAVER_CLIENT_SECRET = NAVER_KEYS[0]["secret"]
else:
    NAVER_CLIENT_ID = None
    NAVER_CLIENT_SECRET = None

# ==========================================
# [검증] 필수 키가 없으면 경고 출력
# ==========================================
if not TELEGRAM_BOT_TOKEN or not KIS_APP_KEY:
    logging.critical("🚨 [경고] .env 파일이 없거나 API 키가 로드되지 않았습니다!")

# ==========================================
# 8. AI 분석 키워드 (기본값 — init_config()에서 DB값으로 in-place 교체)
# ==========================================
AI_TRIGGER_KEYWORDS = [
    "공급계약", "투자판단", "공급", "무상증자", "유상증자", "소송",
    "정지", "해제", "인수", "합병", "양수", "양도",
    "잠정실적", "매출액", "영업이익", "당기순이익", "부도", "횡령", "배임",
    "임상", "특허", "개발", "승인", "신규", "투자"
]

# ==========================================
# 9. 비보유 종목 필수 알림 키워드 (기본값 — init_config()에서 DB값으로 in-place 교체)
# ==========================================
GLOBAL_IMPORTANT_KEYWORDS = [
    "투자판단", "거래정지", "상장폐지", "관리종목", "공개매수",
    "부도", "횡령", "배임", "최대주주변경", "무상증자", "공급계약",
    "투자경고", "투자위험", "투자주의",
]

# 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "companies.json")
DEFAULT_SAVE_DIR = "news_results"

MAX_NEWS_PAGES = 5

# 산업별 통합 채팅방 기본값 — rooms 테이블 최초 시드 + 브릿지 장애 시 폴백 전용.
# 실제 사용 값은 아래 공유 컨테이너 INDUSTRY_CHAT_IDS (init_config()에서 채움).
_DEFAULT_INDUSTRY_CHAT_IDS = {
    "2차전지": "@evbattery",
    "반도체": "@ksemiconchat",
    "로봇": "@krobotchat",
    "조선": "@shipbuildingchat",
    "뷰티": "@kbeautychat",
    "엔터": "@Kenterchat",
    "신재생": "@renewenergychat",
    "바이오": "@Kbiochat",
    "테크": "@ktechchat",
    "소비재": "@consumerchat",
    "우주": "@spacecraftchat",
    "바티인베스트": "@BatiInvestChat"
}

# ==========================================
# 5. 산업별 뉴스 검색 키워드 (기본값 — init_config()에서 DB값으로 in-place 교체)
# ==========================================
INDUSTRY_SEARCH_TERMS = {
    "2차전지": ["양극재", "음극재", "전해액", "전고체 배터리", "LFP 배터리", "폐배터리", "리튬 가격", "IRA 보조금", "4680 배터리", "실리콘음극재"],
    "반도체": ["HBM", "CXL", "파운드리", "DDR5", "반도체 장비", "EUV", "뉴로모픽", "유리기판", "온디바이스AI", "NPU", "낸드 가격"],
    "로봇": ["협동로봇", "휴머노이드", "물류로봇", "로봇 감속기", "삼성 로봇", "스마트팩토리", "AMR", "무인화"],
    "조선": ["LNG선", "신조선가", "선박 수주", "암모니아선", "친환경 선박", "해양플랜트", "선박 엔진"],
    "뷰티": ["화장품 수출", "미용기기", "리쥬란", "인디브랜드", "올리브영", "아마존 뷰티", "홈뷰티", "K뷰티"],
    "엔터": ["앨범 초동", "월드투어", "위버스", "걸그룹 데뷔", "드라마 시청률", "OTT 순위", "넷플릭스 순위"],
    "신재생": ["태양광 발전", "해상풍력", "SMR", "원전 수주", "수소 연료전지", "RE100"],
    "바이오": ["비만치료제", "ADC", "FDA 승인", "면역항암제", "K바이오", "빅파마", "임상 3상", "생물보안법", "의료AI", "방사성의약품"],
    "테크": ["AI 데이터센터", "변압기 수출", "폴더블폰", "XR기기", "생성형 AI", "PCB"],
    "소비재": ["라면 수출", "불닭볶음면", "냉동김밥", "K-푸드", "의류 OEM"],
    "우주": ["인공위성", "스페이스X", "스타링크", "우주항공청", "저궤도 위성", "로켓 발사", "달 탐사", "재사용 로켓", "NASA", "KASA"]
}

KEYWORD_TO_COMPANIES = {
    "방사성의약품": ["퓨쳐켐", "셀비온"],
    "비만치료제": ["펩트론", "인벤티지랩", "디앤디파마텍", "지투지바이오"]
}

COMMON_BUTTON = {
    "inline_keyboard": [[
        {"text": "💰 바티인베스트", "url": "https://t.me/BatiInvestChat"}
    ]]
}

# ==========================================
# 2. 공유 컨테이너 (⚠️ 재바인딩 금지 — in-place 갱신 전용)
#    'from config import X'는 참조를 복사하므로, 이 객체들을 새 dict로
#    교체하면 다른 모듈에 반영되지 않는다. 반드시 clear()+update()로 갱신.
#    (구 sys.modules setattr 해킹을 대체하는 메커니즘)
# ==========================================
COMPANY_CODES: dict = {}        # 종목명 → 코드
COMPANY_KEYWORDS: list = []     # [{name, related_keywords, additional_keywords}]
INDUSTRY_HIERARCHY: dict = {}   # 산업 → {세부섹터: [종목명]}
THEME_MAP: dict = {}            # 세부섹터 → [종목명]
COMPANY_TO_INDUSTRY: dict = {}  # 종목명 → 산업
COMPANY_CHAT_IDS: dict = {}     # 종목명 → chat_id (rooms 테이블)
CHAT_IDS_BY_CODE: dict = {}     # 종목코드 → chat_id (종목명 변경 시 fallback)
INDUSTRY_CHAT_IDS: dict = {}    # 산업 → chat_id (rooms 테이블)

_raw_data: list = []            # 마지막 로드된 companies 원본 (내부용)


# ==========================================
# 3. 데이터 로더
# ==========================================
def load_company_data_from_db():
    """
    Supabase companies 테이블에서 봇 모니터링 종목 로드.
    is_monitored=True (full + news) 종목만 가져옵니다.
    전체 상장사(data 레벨)는 봇이 처리하지 않습니다.
    """
    try:
        from supabase_bridge import bridge as _b
        rows = _b.get_companies(level='monitored')  # full + news만
        if rows:
            logging.info(
                f"✅ [DB] 모니터링 종목 로드: "
                f"full={sum(1 for r in rows if r.get('monitoring_level')=='full')}개, "
                f"news={sum(1 for r in rows if r.get('monitoring_level')=='news')}개"
            )
            return rows
    except Exception as e:
        logging.warning(f"⚠️ [DB] companies 로드 실패, JSON 폴백: {e}")
    return []

def load_company_data_from_json():
    """companies.json에서 종목 데이터 로드 (폴백용)"""
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
            # JSON 구조를 DB 구조로 변환
            result = []
            for item in raw:
                kw_add = ','.join(item.get('keywords', {}).get('additional', []) or [])
                kw_rel = ','.join(item.get('keywords', {}).get('related', []) or [])
                result.append({
                    'name':                item.get('name', ''),
                    'code':                item.get('code', ''),
                    'industry':            item.get('industry', ''),
                    'sub_industry':        item.get('sub_industry', ''),
                    'chat_id':             item.get('chat_id'),
                    'keywords_additional': kw_add,
                    'keywords_related':    kw_rel,
                    'active':              True,
                })
            return result
        except json.JSONDecodeError as e:
            logging.error(f"⛔ JSON 파싱 에러: {e}")
            return []


def _build_company_dicts(data: list) -> tuple:
    """
    raw_data 리스트 → (COMPANY_CODES, COMPANY_KEYWORDS,
                       INDUSTRY_HIERARCHY, THEME_MAP, COMPANY_TO_INDUSTRY)
    DB 컬럼명: keywords_related / keywords_additional 사용.
    """
    codes = {
        item["name"]: item["code"]
        for item in data
        if item.get("code") and item.get("active", True)
    }

    keywords = [
        {
            "name": item["name"],
            "related_keywords":    [k.strip() for k in (item.get("keywords_related")    or "").split(",") if k.strip()],
            "additional_keywords": [k.strip() for k in (item.get("keywords_additional") or "").split(",") if k.strip()],
        }
        for item in data
        if item.get("active", True)
    ]

    hierarchy: dict = {}
    theme_map: dict = {}
    company_to_industry: dict = {}

    for item in data:
        if not item.get("active", True):
            continue
        name = item["name"]
        ind  = item.get("industry")
        sub  = item.get("sub_industry")
        if ind:
            safe_sub = sub or "기타"
            hierarchy.setdefault(ind, {}).setdefault(safe_sub, []).append(name)
            company_to_industry[name] = ind
        if sub:
            theme_map.setdefault(sub, []).append(name)

    return codes, keywords, hierarchy, theme_map, company_to_industry


def _apply_company_data(data: list):
    """빌드 결과를 공유 컨테이너에 in-place 반영."""
    codes, keywords, hierarchy, theme_map, c2i = _build_company_dicts(data)
    COMPANY_CODES.clear();        COMPANY_CODES.update(codes)
    COMPANY_KEYWORDS[:] = keywords
    INDUSTRY_HIERARCHY.clear();   INDUSTRY_HIERARCHY.update(hierarchy)
    THEME_MAP.clear();            THEME_MAP.update(theme_map)
    COMPANY_TO_INDUSTRY.clear();  COMPANY_TO_INDUSTRY.update(c2i)


def _load_rooms(bridge, seed: bool = False):
    """
    rooms 테이블에서 종목/산업 채팅방을 공유 컨테이너에 in-place 로드.

    seed=True  (init): 하드코딩 산업방 기본값을 rooms에 시드한 뒤 DB값만 사용
                       (DB가 비어 있으면 빈 상태 유지 + 경고 — 기존 동작 보존)
    seed=False (reload): DB값이 있을 때만 교체, 실패/빈값이면 기존 값 유지
    """
    by_name = bridge.get_company_chat_ids()
    if by_name:
        COMPANY_CHAT_IDS.clear()
        COMPANY_CHAT_IDS.update(by_name)
        logging.info(f"✅ [Bridge] 채팅방 ID {len(COMPANY_CHAT_IDS)}개 rooms 테이블에서 로드")
    by_code = getattr(bridge, 'chat_ids_by_code', None)
    if by_code:
        CHAT_IDS_BY_CODE.clear()
        CHAT_IDS_BY_CODE.update(by_code)
        logging.info(f"✅ [Bridge] 코드 기준 채팅방 {len(CHAT_IDS_BY_CODE)}개 로드")

    if seed:
        bridge.seed_industry_rooms(_DEFAULT_INDUSTRY_CHAT_IDS)
        INDUSTRY_CHAT_IDS.clear()   # 하드코딩값 제거 — DB만 사용
    db_ind = bridge.get_industry_chat_ids()
    if db_ind:
        INDUSTRY_CHAT_IDS.clear()
        INDUSTRY_CHAT_IDS.update(db_ind)
        logging.info(f"✅ [Bridge] 산업 채팅방 {len(INDUSTRY_CHAT_IDS)}개 rooms 테이블에서 로드")
    elif seed:
        logging.warning("⚠️ [Bridge] rooms 테이블에 산업 채팅방 없음 — 대시보드에서 등록하세요")


# ==========================================
# 4. 초기화 / 재로드 (import 부작용의 명시적 진입점)
# ==========================================

# ── reload 콜백 레지스트리 ─────────────────────────────
# check_reload_flag()는 bridge 싱글톤에 마지막 값을 저장하는 단일 소비자 설계라,
# 여러 곳(공시봇·뉴스봇·워치독)이 각자 폴링하면 먼저 본 쪽만 True를 받는 경쟁이 생긴다.
# → 플래그 소비는 워치독 한 곳으로 통일하고, 모듈별 후속 갱신(필터 재로드·방 매핑
#   재빌드 등)은 여기 등록된 콜백으로 전파한다. key 중복 등록 시 교체(인스턴스 재생성 안전).
_RELOAD_CALLBACKS: dict = {}   # key → callable


def on_reload(fn, key: str = None):
    """reload_company_data() 완료 후 호출될 콜백 등록."""
    _RELOAD_CALLBACKS[key or getattr(fn, '__qualname__', repr(fn))] = fn


def _fire_reload_callbacks():
    for _key, _fn in list(_RELOAD_CALLBACKS.items()):
        try:
            _fn()
        except Exception as _cbe:
            logging.error(f"⚠️ [Reload] 콜백 '{_key}' 실행 실패: {_cbe}")

def init_config():
    """
    설정 전체 로드 — 모듈 하단에서 import 시 1회 자동 호출.
    종목 데이터 + 브릿지(키워드/채팅방/검색어) 오버라이드 + 기본값 시드.
    """
    global _raw_data
    _raw_data = load_company_data_from_db() or load_company_data_from_json()
    logging.info(f"📊 총 {len(_raw_data)}개 종목 로드 완료")
    _apply_company_data(_raw_data)

    # 브릿지 장애 시 폴백: 산업방은 하드코딩 기본값 사용
    INDUSTRY_CHAT_IDS.clear()
    INDUSTRY_CHAT_IDS.update(_DEFAULT_INDUSTRY_CHAT_IDS)

    try:
        from supabase_bridge import bridge as _bridge

        # AI 키워드 — DB 값이 있으면 in-place 교체
        _db_ai_kw = _bridge.get_ai_keywords()
        if _db_ai_kw:
            AI_TRIGGER_KEYWORDS[:] = _db_ai_kw
            logging.info(f"✅ [Bridge] AI 키워드 {len(AI_TRIGGER_KEYWORDS)}개 DB에서 로드")

        # 전체 중요 키워드 — DB 값이 있으면 in-place 교체
        _db_global_kw = _bridge.get_global_keywords()
        if _db_global_kw:
            GLOBAL_IMPORTANT_KEYWORDS[:] = _db_global_kw
            logging.info(f"✅ [Bridge] 전체 키워드 {len(GLOBAL_IMPORTANT_KEYWORDS)}개 DB에서 로드")

        # 종목/산업 채팅방 — rooms 테이블 (시드 포함)
        _load_rooms(_bridge, seed=True)

        # 산업별 뉴스 검색어 — DB 값으로 완전 대체 (merge 아님)
        _db_news_terms = _bridge.get_news_search_terms()
        if _db_news_terms:
            INDUSTRY_SEARCH_TERMS.clear()
            INDUSTRY_SEARCH_TERMS.update(_db_news_terms)
            logging.info(f"✅ [Bridge] 뉴스 검색어 {len(_db_news_terms)}개 산업 DB에서 로드")

        # DB에 없는 기본값 시드 (최초 실행 시 1회)
        _seed_news = {
            f"news_terms_{ind}": ",".join(terms)
            for ind, terms in INDUSTRY_SEARCH_TERMS.items()
        }
        _seed_news.update({
            "ai_trigger_keywords":       ",".join(AI_TRIGGER_KEYWORDS),
            "global_important_keywords": ",".join(GLOBAL_IMPORTANT_KEYWORDS),
        })
        _bridge.seed_defaults(_seed_news)

    except Exception as _e:
        logging.warning(f"⚠️ [Bridge] Supabase 브릿지 로드 실패 (기존 설정 사용): {_e}")


def reload_company_data():
    """
    Supabase companies/rooms 테이블을 다시 읽어 공유 컨테이너를 in-place 갱신.
    대시보드 '봇 설정 재로드' 버튼(reload_flag) 트리거로 호출됩니다.
    'from config import X' 참조가 같은 객체를 공유하므로 별도 전파 불필요.
    """
    global _raw_data

    logging.info("🔄 [Reload] 종목 데이터 재로드 시작...")

    new_data = load_company_data_from_db() or load_company_data_from_json()
    if not new_data:
        logging.warning("⚠️ [Reload] 데이터 없음 — 기존 유지")
        return False

    _raw_data = new_data
    _apply_company_data(_raw_data)

    try:
        from supabase_bridge import bridge as _b
        _load_rooms(_b, seed=False)
    except Exception as _ce:
        logging.warning(f"⚠️ [Reload] rooms 채팅방 로드 실패 (기존 유지): {_ce}")

    _fire_reload_callbacks()   # 봇별 후속 갱신 (공시필터·뉴스필터·리스너 방 매핑 등)

    logging.info(f"✅ [Reload] 완료 — {len(COMPANY_CODES)}개 종목, {len(COMPANY_CHAT_IDS)}개 채팅방")
    return True


# ==========================================
# 모듈 로드 시 1회 초기화 (기존 import 동작 유지)
# ==========================================
init_config()
