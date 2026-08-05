"""
test_dart_rules.py — 공시 분류·라우팅 정책 회귀 테스트

실행: python3 test_dart_rules.py   (외부 의존 없음 — 순수 함수만 검증)
키워드를 app_config에서 바꾸기 전/후 정책 회귀 확인용.
※ import 시 DB 로드 없음 — 코드 기본값 기준으로 검증.
"""
from dart_rules import classify_disclosure, decide_targets

FAIL = []


def eq(got, exp, label):
    if got != exp:
        FAIL.append(f"{label}: got={got!r} exp={exp!r}")


# ══════════════════════════════════════════════
#  1. 등급 분류
# ══════════════════════════════════════════════
CLASSIFY_CASES = {
    # urgent
    "주권매매거래정지":                        "urgent",
    "상장폐지결정":                            "urgent",
    "관리종목지정":                            "urgent",
    "횡령ㆍ배임혐의발생":                      "urgent",
    "공개매수결과보고서":                      "urgent",   # skip '결과보고서'류와 겹쳐도 긴급 우선
    "회생절차개시신청":                        "urgent",
    "파산신청":                                "urgent",
    "감자결정(무상감자)":                      "urgent",
    "기타시장안내(상장적격성 실질심사 사유 발생)": "urgent",
    # skip (산업/메인 제외, 기업채널만)
    "주주총회소집공고":                        "skip",
    "감사보고서제출":                          "skip",
    "임원ㆍ주요주주특정증권등소유상황보고서":  "skip",
    "자기주식취득결과보고서":                  "skip",
    # major
    "단일판매ㆍ공급계약체결":                  "major",
    "유상증자결정":                            "major",
    "무상증자결정":                            "major",
    "전환사채권발행결정":                      "major",
    "최대주주변경":                            "major",
    "영업양수도결정":                          "major",
    "주식소각결정":                            "major",
    "현금ㆍ현물배당결정":                      "major",
    "소송등의제기ㆍ신청":                      "major",
    "연결재무제표기준영업(잠정)실적(공정공시)": "major",
    # 관리종목 지정'우려'(사전경고)는 major — '관리종목' URGENT 매칭보다 먼저
    # '지정우려' 가드가 잡음. 실제 '관리종목지정'(line 25)은 urgent 유지.
    "기타시장안내(관리종목지정우려종목)":       "major",
    # normal
    "본점소재지변경":                          "normal",
    "타법인주식및출자증권취득결정":            "normal",
    "기업가치제고계획":                        "normal",
}
for nm, exp in CLASSIFY_CASES.items():
    eq(classify_disclosure(nm), exp, f"classify[{nm}]")


# ══════════════════════════════════════════════
#  2. 채널 라우팅
# ══════════════════════════════════════════════
MAIN, IND, COMP = "@main", "@ind", "@comp"


def targets(level, nm="테스트공시", market_wide=False,
            cap_main=True, cap_large=False, ind=IND, comp=COMP):
    return decide_targets(level, main_chat=MAIN, ind_chat=ind, comp_chat=comp,
                          is_market_wide=market_wide, report_nm=nm,
                          cap_ok_main=cap_main, cap_ok_large=cap_large)


# urgent: 메인(시총 관문) + 산업 + 기업
eq(targets("urgent"), [MAIN, IND, COMP], "urgent 기본")
eq(targets("urgent", cap_main=False), [IND, COMP], "urgent 소형주 메인 제외")
# skip: 기업만
eq(targets("skip"), [COMP], "skip 기업만")
eq(targets("skip", comp=None), [], "skip 기업방 없음")
# normal: 산업 + 기업 (메인 없음)
eq(targets("normal"), [IND, COMP], "normal")
# major 기본: 산업 + 기업
eq(targets("major", "유상증자결정"), [IND, COMP], "major 기본 메인 제외")
# major 공급계약·수주 → 메인 (1000억↑)
eq(targets("major", "단일판매ㆍ공급계약체결"), [IND, COMP, MAIN], "major 공급계약 메인 포함")
eq(targets("major", "단일판매ㆍ공급계약체결", cap_main=False), [IND, COMP], "major 공급계약 소형주 제외")
eq(targets("major", "[기재정정]단일판매ㆍ공급계약체결"), [IND, COMP], "major 기재정정 공급계약 메인 제외")
# major 시장속보 → 메인
eq(targets("major", "유상증자결정", market_wide=True), [IND, COMP, MAIN], "major 시장속보 메인 포함")
# 시총 가중: 대형주(1조↑) 구조적 이벤트 → 메인
eq(targets("major", "유상증자결정", cap_large=True), [IND, COMP, MAIN], "major 대형주 유상증자 메인 승격")
eq(targets("major", "합병결정", cap_large=True), [IND, COMP, MAIN], "major 대형주 합병 메인 승격")
eq(targets("major", "[기재정정]유상증자결정", cap_large=True), [IND, COMP], "major 대형주 기재정정 승격 제외")
eq(targets("major", "특허권취득", cap_large=True), [IND, COMP], "major 대형주 일상성(특허) 승격 제외")
eq(targets("major", "소송등의제기ㆍ신청", cap_large=True), [IND, COMP], "major 대형주 소송 승격 제외")
# 중복 제거 (산업방 = 기업방 동일 설정)
eq(targets("urgent", ind="@same", comp="@same"), [MAIN, "@same"], "동일 채널 dedup")
eq(targets("normal", ind=None), [COMP], "산업방 없음")

# 기재정정 — major는 시장속보 메인 제외, urgent는 메인 유지
eq(targets("major", "[기재정정]무상증자결정", market_wide=True), [IND, COMP],
   "major 기재정정 시장속보 메인 제외")
eq(targets("major", "[기재정정]단일판매ㆍ공급계약체결", market_wide=True), [IND, COMP],
   "major 기재정정 공급계약 시장속보 메인 제외")
eq(targets("major", "[기재정정]유상증자결정", market_wide=True, cap_large=True), [IND, COMP],
   "major 기재정정 대형주 메인 제외")
eq(targets("major", "무상증자결정", market_wide=True), [IND, COMP, MAIN],
   "major 정정아님 시장속보 메인 유지 (회귀 가드)")
eq(targets("urgent", "[기재정정]주권매매거래정지", market_wide=True), [MAIN, IND, COMP],
   "urgent 기재정정 메인 유지")

# 임상시험결과 — 대형주(1조↑)면 메인 추가, 계획승인·개시는 제외
eq(targets("major", "투자판단관련주요경영사항(임상시험결과)", cap_large=True), [IND, COMP, MAIN],
   "major 대형주 임상시험결과 메인 추가")
eq(targets("major", "투자판단관련주요경영사항(임상시험결과)"), [IND, COMP],
   "major 소형주 임상시험결과 메인 제외")
eq(targets("major", "투자판단관련주요경영사항(임상시험계획승인신청)", cap_large=True), [IND, COMP],
   "major 대형주 임상 계획승인 메인 제외 (결과 아님)")
eq(targets("major", "[기재정정]투자판단관련주요경영사항(임상시험결과)", cap_large=True), [IND, COMP],
   "major 임상시험결과 기재정정 메인 제외")


# ══════════════════════════════════════════════
#  3. 운영값(app_config) 드리프트 점검
# ══════════════════════════════════════════════
# 위 케이스는 모두 '코드 기본값' 기준이다. 실운영은 app_config 값이 코드 기본값을
# 덮어쓰므로, 둘이 어긋나면 테스트는 통과해도 운영 동작은 다르다.
#   실제 사고(2026-07-22): app_config dart_urgent에서 횡령·배임이 빠져 있어
#   테스트는 "횡령ㆍ배임=urgent" 통과, 운영은 normal로 분류 → 메인 미발송.
# 코드 기본 키워드가 운영값에서 빠졌으면 실패 처리한다. 의도적으로 뺀 경우라면
# 코드 기본값에서도 제거해 단일 출처를 유지할 것.
# DB 접근 불가 환경에서는 조용히 건너뜀.
import dart_rules as _R

_DEFAULTS = {
    'dart_urgent':     list(_R.URGENT_KEYWORDS),
    'dart_major':      list(_R.MAJOR_KEYWORDS),
    'dart_skip':       list(_R.SKIP_FOR_BROADCAST),
    'dart_major_main': list(_R.MAIN_MAJOR_KEYWORDS),
}
_LIVE = None
try:
    from supabase_bridge import bridge as _b
    if _b._get_client():
        _R.load_dart_filters()
        _LIVE = {
            'dart_urgent':     _R.URGENT_KEYWORDS,
            'dart_major':      _R.MAJOR_KEYWORDS,
            'dart_skip':       _R.SKIP_FOR_BROADCAST,
            'dart_major_main': _R.MAIN_MAJOR_KEYWORDS,
        }
except Exception as e:
    print(f"ℹ️  app_config 미접근 — 드리프트 점검 생략 ({type(e).__name__})")

if _LIVE is not None:
    # 커버리지 판정: 분류는 부분일치(k in report_nm)라, 운영값에 더 짧은 키워드가
    # 있으면 긴 코드 기본값은 이미 커버됨(예: 운영 '소유상황보고' ⊃ 코드
    # '임원ㆍ주요주주특정증권등소유상황보고서') → 누락으로 보지 않는다.
    def _covered(word: str, live: list) -> bool:
        return any(l in word for l in live)

    drift = {k: [w for w in _DEFAULTS[k] if not _covered(w, _LIVE[k])]
             for k in _DEFAULTS}
    drift = {k: v for k, v in drift.items() if v}
    if drift:
        for k, miss in drift.items():
            FAIL.append(f"app_config '{k}'에 코드 기본 키워드 누락: {', '.join(miss)}")
    else:
        print("✅ app_config 드리프트 없음 — 코드 기본 키워드 전부 운영값에 포함")


# ══════════════════════════════════════════════
if FAIL:
    print(f"❌ FAIL {len(FAIL)}건")
    for f in FAIL:
        print(" -", f)
    raise SystemExit(1)
print(f"✅ test_dart_rules OK — classify {len(CLASSIFY_CASES)} + routing 26 케이스 통과")
