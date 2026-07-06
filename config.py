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
# 10. [신규] 구글 스프레드 시트 설정 (일정 관리)
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
# 8. AI 분석 키워드 설정 (기본값 — DB에서 덮어씀)
# ==========================================
AI_TRIGGER_KEYWORDS = [
    "공급계약", "투자판단", "공급", "무상증자", "유상증자", "소송",
    "정지", "해제", "인수", "합병", "양수", "양도",
    "잠정실적", "매출액", "영업이익", "당기순이익", "부도", "횡령", "배임",
    "임상", "특허", "개발", "승인", "신규", "투자"
]

# ==========================================
# 9. 비보유 종목 필수 알림 키워드 (기본값 — DB에서 덮어씀)
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

# 산업별 통합 채팅방 ID
INDUSTRY_CHAT_IDS = {
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
# 2. 데이터 로드 — Supabase DB 우선, 실패 시 companies.json 폴백
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

# DB → JSON 순으로 로드
_raw_data = load_company_data_from_db() or load_company_data_from_json()
logging.info(f"📊 총 {len(_raw_data)}개 종목 로드 완료")

# ==========================================
# 3. 데이터 구조 변환 헬퍼 + 전역 변수 생성
# ==========================================

def _build_company_dicts(data: list) -> tuple:
    """
    raw_data 리스트 → (COMPANY_CODES, COMPANY_KEYWORDS,
                       INDUSTRY_HIERARCHY, THEME_MAP, COMPANY_TO_INDUSTRY)
    초기화와 reload_company_data() 양쪽에서 공유.
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


COMPANY_CODES, COMPANY_KEYWORDS, INDUSTRY_HIERARCHY, THEME_MAP, COMPANY_TO_INDUSTRY = \
    _build_company_dicts(_raw_data)

COMPANY_CHAT_IDS = {}  # rooms 테이블에서 로드 (아래 Bridge 블록에서 덮어씀)
CHAT_IDS_BY_CODE = {}  # stock_code → chat_id (종목명 변경 시 fallback용)

# ==========================================
# 5. 산업별 뉴스 검색 키워드
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

# ==========================================
# 12. [신규] 런타임 종목 데이터 재로드 함수
#     봇 재시작 없이 DB 변경사항을 반영할 때 호출
# ==========================================
def reload_company_data():
    """
    Supabase companies 테이블을 다시 읽어서 전역 변수를 갱신합니다.
    대시보드에서 '봇 설정 재로드' 버튼을 누를 때 트리거됩니다.
    """
    global _raw_data, COMPANY_CODES, COMPANY_KEYWORDS, COMPANY_CHAT_IDS
    global INDUSTRY_HIERARCHY, THEME_MAP, COMPANY_TO_INDUSTRY
    global CHAT_IDS_BY_CODE, INDUSTRY_CHAT_IDS

    logging.info("🔄 [Reload] 종목 데이터 재로드 시작...")

    new_data = load_company_data_from_db() or load_company_data_from_json()
    if not new_data:
        logging.warning("⚠️ [Reload] 데이터 없음 — 기존 유지")
        return False

    _raw_data = new_data

    COMPANY_CODES, COMPANY_KEYWORDS, INDUSTRY_HIERARCHY, THEME_MAP, COMPANY_TO_INDUSTRY = \
        _build_company_dicts(_raw_data)

    COMPANY_CHAT_IDS = {}  # rooms 테이블에서 아래에서 덮어씀

    # rooms 테이블에서 채팅방 ID 덮어쓰기 (companies.chat_id보다 우선)
    try:
        from supabase_bridge import bridge as _b
        _rooms_chat = _b.get_company_chat_ids()
        if _rooms_chat:
            COMPANY_CHAT_IDS = _rooms_chat
            logging.info(f"✅ [Reload] 채팅방 ID {len(COMPANY_CHAT_IDS)}개 rooms 테이블에서 로드")
        if hasattr(_b, 'chat_ids_by_code') and _b.chat_ids_by_code:
            CHAT_IDS_BY_CODE = _b.chat_ids_by_code
            logging.info(f"✅ [Reload] 코드 기준 채팅방 {len(CHAT_IDS_BY_CODE)}개 갱신")
        _rooms_ind = _b.get_industry_chat_ids()
        if _rooms_ind:
            INDUSTRY_CHAT_IDS = _rooms_ind
            logging.info(f"✅ [Reload] 산업 채팅방 {len(INDUSTRY_CHAT_IDS)}개 rooms 테이블에서 로드")
    except Exception as _ce:
        logging.warning(f"⚠️ [Reload] rooms 채팅방 로드 실패: {_ce}")

    logging.info(f"✅ [Reload] 완료 — {len(COMPANY_CODES)}개 종목, {len(COMPANY_CHAT_IDS)}개 채팅방")

    # ── 모든 하위 모듈 전역변수 직접 갱신 ─────────────────────────
    # 'from config import X' 방식은 값을 복사하므로 config 교체만으로는
    # stock_api / news_main / main 등에 반영되지 않음.
    # sys.modules를 통해 각 모듈의 네임스페이스를 직접 업데이트.
    try:
        import sys
        _reload_map = {
            'COMPANY_CODES':       COMPANY_CODES,
            'COMPANY_KEYWORDS':    COMPANY_KEYWORDS,
            'COMPANY_CHAT_IDS':    COMPANY_CHAT_IDS,
            'INDUSTRY_CHAT_IDS':   INDUSTRY_CHAT_IDS,
            'INDUSTRY_HIERARCHY':  INDUSTRY_HIERARCHY,
            'THEME_MAP':           THEME_MAP,
            'COMPANY_TO_INDUSTRY': COMPANY_TO_INDUSTRY,
        }
        for mod_name in ('stock_api', 'news_main', 'main', 'run_all', 'realtime_alert'):
            if mod_name in sys.modules:
                mod = sys.modules[mod_name]
                updated = []
                for var, val in _reload_map.items():
                    if hasattr(mod, var):
                        setattr(mod, var, val)
                        updated.append(var)
                if updated:
                    logging.info(f"✅ [Reload] {mod_name} 갱신: {', '.join(updated)}")
    except Exception as _se:
        logging.warning(f"⚠️ [Reload] 모듈 갱신 실패: {_se}")

    return True

COMMON_BUTTON = {
    "inline_keyboard": [[
        {"text": "💰 바티인베스트", "url": "https://t.me/BatiInvestChat"}
    ]]
}

# ==========================================
# ✅ [추가] Supabase 브릿지 연동
#    DB에서 키워드·채팅방ID를 읽어 덮어씁니다.
#    브릿지 연결 실패 시 기존 하드코딩 값을 그대로 사용합니다.
# ==========================================
try:
    from supabase_bridge import bridge as _bridge

    # AI 키워드 — DB 값이 있으면 덮어쓰기
    _db_ai_kw = _bridge.get_ai_keywords()
    if _db_ai_kw:
        AI_TRIGGER_KEYWORDS = _db_ai_kw
        logging.info(f"✅ [Bridge] AI 키워드 {len(AI_TRIGGER_KEYWORDS)}개 DB에서 로드")

    # 전체 중요 키워드 — DB 값이 있으면 덮어쓰기
    _db_global_kw = _bridge.get_global_keywords()
    if _db_global_kw:
        GLOBAL_IMPORTANT_KEYWORDS = _db_global_kw
        logging.info(f"✅ [Bridge] 전체 키워드 {len(GLOBAL_IMPORTANT_KEYWORDS)}개 DB에서 로드")

    # rooms 테이블에서 COMPANY_CHAT_IDS 덮어쓰기
    # companies.chat_id는 대부분 비어있고 rooms 테이블에 실제 채팅방 ID가 있음
    _db_chat_ids = _bridge.get_company_chat_ids()
    if _db_chat_ids:
        COMPANY_CHAT_IDS = _db_chat_ids
        logging.info(f"✅ [Bridge] 채팅방 ID {len(COMPANY_CHAT_IDS)}개 rooms 테이블에서 로드")
    # 코드 기준 fallback 딕셔너리 (종목명 변경 시 corp_name 매칭 실패 대비)
    if hasattr(_bridge, 'chat_ids_by_code') and _bridge.chat_ids_by_code:
        CHAT_IDS_BY_CODE = _bridge.chat_ids_by_code
        logging.info(f"✅ [Bridge] 코드 기준 채팅방 {len(CHAT_IDS_BY_CODE)}개 로드")

    # 산업 채팅방 — 하드코딩값을 rooms 테이블에 시드 후 DB로만 로드
    _bridge.seed_industry_rooms(INDUSTRY_CHAT_IDS)
    INDUSTRY_CHAT_IDS = {}  # 하드코딩값 제거, DB만 사용
    _db_ind_chat = _bridge.get_industry_chat_ids()
    if _db_ind_chat:
        INDUSTRY_CHAT_IDS = _db_ind_chat
        logging.info(f"✅ [Bridge] 산업 채팅방 {len(INDUSTRY_CHAT_IDS)}개 rooms 테이블에서 로드")
    else:
        logging.warning("⚠️ [Bridge] rooms 테이블에 산업 채팅방 없음 — 대시보드에서 등록하세요")

    # 산업별 뉴스 검색어 — DB 값으로 완전 대체 (merge 아님)
    _db_news_terms = _bridge.get_news_search_terms()
    if _db_news_terms:
        INDUSTRY_SEARCH_TERMS = _db_news_terms
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
