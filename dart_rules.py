"""
dart_rules.py — 공시 중요도 분류·채널 라우팅 정책 (순수 로직)

main.py(수집·발송 오케스트레이션)에서 정책만 분리한 모듈.
classify_disclosure / decide_targets 는 부수효과 없는 순수 함수라
test_dart_rules.py 로 회귀 테스트 가능.

키워드·필터는 app_config 이 단일 출처(load_dart_filters 로 로드).
컨테이너는 in-place 갱신하므로 'from dart_rules import X' 참조가 안전
(config.py 공유 컨테이너와 동일 규약).

등급 → 채널:
  URGENT  → 메인(시총 1000억↑) + 산업 + 기업
  MAJOR   → 산업 + 기업 (+ 공급계약·수주/시장속보/대형주 구조 이벤트는 메인)
  NORMAL  → 산업 + 기업
  SKIP    → 기업만
"""
import logging

# ══════════════════════════════════════════════
#  키워드 기본값 (app_config에서 덮어씀)
# ══════════════════════════════════════════════

URGENT_KEYWORDS = [
    "거래정지", "매매거래정지",
    "상장폐지", "관리종목",
    "횡령", "배임",
    "공개매수",
    "불성실공시",
    "영업정지",
    "회생절차", "파산", "감자",
    "상장적격성",   # 상장적격성 실질심사 사유 발생/결과 — 상폐 심사 단계
]

MAJOR_KEYWORDS = [
    "공급계약", "수주",
    # 실제 제목은 "영업(잠정)실적" — '잠정실적' 연속 매칭 불가라 '잠정' 사용
    "잠정", "매출액",
    "무상증자",
    "유상증자",
    "최대주주변경",
    "합병", "분할", "인수",
    "전환사채", "신주인수권부사채",
    "소송", "분쟁",
    "특허", "임상",
    "사업보고서", "분기보고서", "반기보고서",
    "영업양수", "영업양도", "주식소각", "액면", "배당",
]

# 산업/메인 채널에서 스킵 (기업채널은 정상 발송)
SKIP_FOR_BROADCAST = [
    "주식등의대량보유상황보고",
    "임원ㆍ주요주주특정증권등소유상황보고서",
    "임원·주요주주특정증권등소유상황보고서",
    "소유상황보고",
    "기업설명회",
    "IR개최",
    "감사보고서",
    "주주총회소집공고",
    "주주총회결과",
    "의결권대리행사권유",
    "증권발행실적보고서",  # 증자 완료 후 결과 보고
    "투자설명서",          # 공모 관련 중간 서류
    "자기주식취득결과보고서",
    "자기주식처분결과보고서",
]

# 대형주(시총 1조↑) major를 메인 채널로 승격할 구조적 이벤트
# (분기보고서·특허·소송 등 일상성 major는 제외 — 메인 노이즈 방지)
MAIN_MAJOR_KEYWORDS = [
    "유상증자", "무상증자", "합병", "분할", "최대주주변경",
    "영업양수", "영업양도", "잠정",
    "전환사채", "신주인수권부사채", "주식소각",
]

# 기업 필터 (app_config에서 로드 — in-place 갱신)
DART_BLACKLIST: set = set()    # 기업명 정확 일치 차단
DART_TITLE_FILTER: list = []   # 공시 제목 부분일치 차단
DART_CORP_FILTER: list = []    # 기업명 부분일치 차단

# 시총 임계값
CAP_MAIN  = 100_000_000_000      # 메인 채널 최소 시총: 1000억
CAP_LARGE = 1_000_000_000_000    # 대형주 major 메인 승격: 1조


# ══════════════════════════════════════════════
#  순수 함수 — 분류·라우팅
# ══════════════════════════════════════════════

def classify_disclosure(report_nm: str) -> str:
    """
    공시 중요도 분류.
    반환: 'urgent' | 'major' | 'skip' | 'normal'
    """
    # 긴급 최우선 — skip 키워드와 한 제목에서 겹칠 때 긴급이 이겨야 함
    # (키워드는 app_config에서 운영 변경되므로 겹침을 전제로 방어)
    if any(k in report_nm for k in URGENT_KEYWORDS):
        return 'urgent'
    if any(k in report_nm for k in SKIP_FOR_BROADCAST):
        return 'skip'
    if any(k in report_nm for k in MAJOR_KEYWORDS):
        return 'major'
    return 'normal'


def decide_targets(level: str, *, main_chat: str, ind_chat: str | None,
                   comp_chat: str | None, is_market_wide: bool,
                   report_nm: str, cap_ok_main: bool, cap_ok_large: bool) -> list:
    """
    등급 → 발송 채널 목록 (순서 보존, 중복 제거).

    cap_ok_main : 시총 1000억↑ 또는 시총 정보 없음 (메인 채널 기본 관문)
    cap_ok_large: 시총 1조↑ 확인됨 (대형주 major 메인 승격 — 정보 없으면 False)
    """
    targets = []
    if level == 'urgent':
        # 긴급: 메인(시총 관문) + 산업 + 기업
        if cap_ok_main:
            targets.append(main_chat)
        if ind_chat:
            targets.append(ind_chat)
        if comp_chat:
            targets.append(comp_chat)
    elif level == 'major':
        # 중요: 산업 + 기업 (+ 아래 조건 충족 시 메인)
        if ind_chat:
            targets.append(ind_chat)
        if comp_chat:
            targets.append(comp_chat)
        # major 기재정정은 메인(시장속보) 제외 — 정정은 '새 속보'가 아니라
        # 과거 공시의 정정이라 시장속보 프레임과 맞지 않고, 비보유 종목이면
        # 원공시 맥락도 없음. 보유 종목 공급계약 정정을 빼던 규칙을 시장속보에도 통일.
        # (urgent 정정은 사안 자체가 중대하므로 urgent 분기에서 그대로 메인 유지)
        is_amend = "기재정정" in report_nm
        to_main = (is_market_wide and not is_amend) or (
            not is_amend
            and any(k in report_nm for k in ("공급계약", "수주")))
        # 시총 가중: 대형주(1조↑)의 구조적 이벤트(증자·합병 등)는 메인 포함
        if not to_main and cap_ok_large and not is_amend \
                and any(k in report_nm for k in MAIN_MAJOR_KEYWORDS):
            to_main = True
        if to_main and cap_ok_main:
            targets.append(main_chat)
    elif level == 'skip':
        # 잡공시: 기업채널만
        if comp_chat:
            targets.append(comp_chat)
    else:  # normal
        # 일반: 산업 + 기업 (메인 제외)
        if ind_chat:
            targets.append(ind_chat)
        if comp_chat:
            targets.append(comp_chat)
    # 같은 방 중복 발송 방지 (산업방=기업방 동일 설정 등) — 순서 보존 dedup
    return list(dict.fromkeys(targets))


# ══════════════════════════════════════════════
#  app_config 로드 (유일한 부수효과 함수)
# ══════════════════════════════════════════════

def load_dart_filters():
    """app_config에서 공시 등급 키워드 + 각종 필터 로드. DB에 없는 기본값은 최초 1회 시드.
    컨테이너는 in-place 갱신 — 'from dart_rules import X' 참조 유지."""
    try:
        from supabase_bridge import bridge as _bridge
    except Exception:
        return
    _bridge.seed_defaults({
        "dart_urgent":     ",".join(URGENT_KEYWORDS),
        "dart_major":      ",".join(MAJOR_KEYWORDS),
        "dart_skip":       ",".join(SKIP_FOR_BROADCAST),
        "dart_major_main": ",".join(MAIN_MAJOR_KEYWORDS),
    })
    try:
        client = _bridge._get_client()
        if not client:
            return
        keys = ['dart_blacklist', 'dart_urgent', 'dart_major', 'dart_skip',
                'dart_major_main', 'dart_title_filter', 'dart_corp_filter']
        res = client.table('app_config').select('key,value').in_('key', keys).execute()
        cfg = {r['key']: r['value'] for r in (res.data or [])}

        def _csv(key):
            return [k.strip() for k in (cfg.get(key) or '').split(',') if k.strip()]

        if kws := _csv('dart_blacklist'):
            DART_BLACKLIST.clear()
            DART_BLACKLIST.update(kws)
            logging.info(f"✅ [공시봇] 블랙리스트 {len(kws)}개 로드")
        if kws := _csv('dart_urgent'):
            URGENT_KEYWORDS[:] = kws
            logging.info(f"✅ [공시봇] 긴급 키워드 {len(kws)}개 로드")
        if kws := _csv('dart_major'):
            MAJOR_KEYWORDS[:] = kws
            logging.info(f"✅ [공시봇] 중요 키워드 {len(kws)}개 로드")
        if kws := _csv('dart_skip'):
            SKIP_FOR_BROADCAST[:] = kws
            logging.info(f"✅ [공시봇] 잡공시 키워드 {len(kws)}개 로드")
        if kws := _csv('dart_major_main'):
            MAIN_MAJOR_KEYWORDS[:] = kws
            logging.info(f"✅ [공시봇] 대형주 메인승격 키워드 {len(kws)}개 로드")
        if kws := _csv('dart_title_filter'):
            DART_TITLE_FILTER[:] = kws
            logging.info(f"✅ [공시봇] 제목 필터 {len(kws)}개 로드")
        if kws := _csv('dart_corp_filter'):
            DART_CORP_FILTER[:] = kws
            logging.info(f"✅ [공시봇] 기업명 필터 {len(kws)}개 로드")

    except Exception as e:
        logging.warning(f"⚠️ [공시봇] 필터 로드 실패 (기본값 사용): {e}")


# reload_flag 소비는 watchdog 단일 창구 — 재로드 시 공시 필터도 함께 갱신되도록 콜백 등록.
try:
    from config import on_reload as _on_reload
    _on_reload(load_dart_filters)
except Exception:
    pass
