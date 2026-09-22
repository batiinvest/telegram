"""
collect_wics.py
───────────────
네이버 증권 → WICS 업종 분류 → companies.wics_industry / wics_code

[왜 이 소스인가]
  companies.industry는 '테마'(바이오·2차전지·로봇, 11종)라 전 종목에 붙일 성격이 아니고,
  실제로 절반(1,300/2,661)이 비어 있었다. 업종은 별개 축이 필요한데,
  - KIS bstp_kor_isnm : 이미 받고 있으나 너무 거칠다(삼성전자·LG전자·삼성SDI가 모두 '전기·전자')
  - DART sector(KSIC) : 문구가 길고 투자용 분류가 아니다(백엔드 알림용으로만 유지)
  - 토스 공식 OpenAPI : 스펙에 업종 필드 자체가 없음(2026-09 확인)
  → 네이버·토스가 공통으로 쓰는 WICS(GICS 호환)를 채택. 네이버는 업종별 묶음 조회라
    종목당 호출이 필요 없어 전 종목을 ~200회로 끝낸다.

[API] (비공식 — 앱 내부용. 실패해도 기존 값이 남아 화면은 비지 않는다)
  GET m.stock.naver.com/api/stocks/industry/{no}?page=&pageSize=100
    → groupInfo{no,name,totalCount} + stocks[{itemCode,...}]

[쓰기 방식 — 중요]
  companies.code에는 UNIQUE 제약이 없어 on_conflict=code upsert가 42P10으로 실패한다.
  대신 업종별로 코드를 묶어 PATCH(code=in.(...))한다 → 전체 갱신이 79회 PATCH로 끝난다.

실행:
  python3 collect_wics.py            # 수집 + 반영
  python3 collect_wics.py --dry      # 수집만, 쓰기 없음
"""

import sys
import time
from typing import Dict, List

import requests
from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = get_logger(__name__)

NAVER_URL = "https://m.stock.naver.com/api/stocks/industry/{no}"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://m.stock.naver.com/",
}

# 업종 그룹 번호 탐색 범위. 실측(2026-09) 261~339에 분포하며 여유를 둔다.
# 매번 훑어 신규 업종이 생겨도 자동 반영되게 한다(호출 100여 회, 20초 수준).
SCAN_FROM, SCAN_TO = 250, 360

# 전 종목을 담는 잡동사니 버킷(ETF·ETN 등 1,500여 개). 업종으로 쓰지 않는다.
SKIP_GROUPS = {"기타"}

# 업종명 → WICS 코드. WICS 코드는 불변이라 상수로 고정한다(매일 토스를 두드릴 이유가 없다).
# 새 업종명이 나타나면 코드 없이 이름만 저장하고 경고를 남긴다.
WICS_CODES: Dict[str, str] = {
    '에너지장비및서비스': 'G101010', '석유와가스': 'G101020',
    '화학': 'G151010', '포장재': 'G151030', '비철금속': 'G151040',
    '철강': 'G151050', '종이와목재': 'G151060',
    '우주항공과국방': 'G201010', '건축제품': 'G201020', '건축자재': 'G201025',
    '건설': 'G201030', '가구': 'G201035', '전기장비': 'G201040',
    '복합기업': 'G201050', '기계': 'G201060', '조선': 'G201065',
    '무역회사와판매업체': 'G201070', '상업서비스와공급품': 'G202010',
    '항공화물운송과물류': 'G203010', '항공사': 'G203020', '해운사': 'G203030',
    '도로와철도운송': 'G203040', '운송인프라': 'G203050',
    '자동차부품': 'G251010', '자동차': 'G251020',
    '가정용기기와용품': 'G252040', '레저용장비와제품': 'G252050',
    '섬유,의류,신발,호화품': 'G252060', '화장품': 'G252065', '문구류': 'G252070',
    '호텔,레스토랑,레저': 'G253010', '다각화된소비자서비스': 'G253020',
    '판매업체': 'G255010', '인터넷과카탈로그소매': 'G255020',
    '백화점과일반상점': 'G255030', '전문소매': 'G255040', '교육서비스': 'G256010',
    '식품과기본식료품소매': 'G301010', '음료': 'G302010', '식품': 'G302020',
    '담배': 'G302030', '가정용품': 'G303010',
    '건강관리장비와용품': 'G351010', '건강관리업체및서비스': 'G351020',
    '건강관리기술': 'G351030', '생물공학': 'G352010', '제약': 'G352020',
    '생명과학도구및서비스': 'G352030',
    '은행': 'G401010', '증권': 'G402010', '창업투자': 'G403020', '카드': 'G403030',
    '기타금융': 'G403040', '손해보험': 'G404010', '생명보험': 'G404020', '부동산': 'G405020',
    'IT서비스': 'G451020', '소프트웨어': 'G451030', '통신장비': 'G452010',
    '핸드셋': 'G452015', '컴퓨터와주변기기': 'G452020', '전자장비와기기': 'G452030',
    '사무용전자제품': 'G452040', '반도체와반도체장비': 'G453010',
    '전자제품': 'G453510', '전기제품': 'G453520',
    '디스플레이패널': 'G454010', '디스플레이장비및부품': 'G454020',
    '다각화된통신서비스': 'G501010', '무선통신서비스': 'G501020',
    '광고': 'G502010', '방송과엔터테인먼트': 'G502020', '출판': 'G502030',
    '게임엔터테인먼트': 'G502040', '양방향미디어와서비스': 'G502050',
    '전기유틸리티': 'G551010', '가스유틸리티': 'G551020', '복합유틸리티': 'G551030',
}

# 부분 수집분을 그대로 쓰면 나머지 종목이 옛 값으로 남아 조용히 어긋난다.
# 아래 기준에 못 미치면 쓰지 않고 중단한다(데이터 손실 없이 다음 회차 재시도).
MIN_GROUPS = 60      # 실측 78종
MIN_CODES  = 2000    # 실측 2,869종(기타 제외)

PATCH_CHUNK = 150    # code=in.(...) URL 길이 안전선 (171개=1,277자 실측)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_industry_map() -> Dict[str, str]:
    """전 종목 code → 업종명. 실패한 그룹은 건너뛰고 계속한다."""
    s = _session()
    mapping: Dict[str, str] = {}
    groups = 0

    for no in range(SCAN_FROM, SCAN_TO + 1):
        try:
            j = s.get(NAVER_URL.format(no=no), params={"page": 1, "pageSize": 100}, timeout=15).json()
        except Exception:
            continue
        gi = j.get("groupInfo") or {}
        name, total = gi.get("name"), gi.get("totalCount") or 0
        if not name or not total or name in SKIP_GROUPS:
            continue

        groups += 1
        got = 0
        page = 1
        while True:
            for x in (j.get("stocks") or []):
                code = (x.get("itemCode") or "").strip()
                if code:
                    mapping[code] = name
            got += len(j.get("stocks") or [])
            if got >= total or not (j.get("stocks") or []) or page > 30:
                break
            page += 1
            try:
                j = s.get(NAVER_URL.format(no=no), params={"page": page, "pageSize": 100}, timeout=15).json()
            except Exception:
                break
        time.sleep(0.05)

    log.info(f"[WICS] 네이버 수집: 업종 {groups}종 / 종목 {len(mapping)}개")
    return mapping


def _our_codes(sb) -> Dict[str, str]:
    """companies의 code → 정규화 코드. .KS/.KQ 접미사 방어(현재는 없으나 과거 이력 있음)."""
    rows = fetch_all_pages(
        sb.table("companies").select("code").eq("active", True).order("code")
    )
    out = {}
    for r in rows:
        raw = (r.get("code") or "").strip()
        if raw:
            out[raw.split(".")[0].strip()] = raw
    return out


def apply_map(sb, mapping: Dict[str, str], dry: bool = False) -> int:
    """업종별로 묶어 PATCH. companies.code에 UNIQUE가 없어 upsert는 쓸 수 없다."""
    ours = _our_codes(sb)

    by_ind: Dict[str, List[str]] = {}
    for norm_code, ind in mapping.items():
        raw = ours.get(norm_code)
        if raw:                      # 우리 테이블에 있는 종목만 갱신
            by_ind.setdefault(ind, []).append(raw)

    hit = sum(len(v) for v in by_ind.values())
    log.info(f"[WICS] 우리 종목 매칭: {hit}/{len(ours)} (업종 {len(by_ind)}종)")

    unknown = [i for i in by_ind if i not in WICS_CODES]
    if unknown:
        log.warning(f"[WICS] 코드 미등록 업종(이름만 저장): {unknown}")

    if dry:
        return hit

    updated = 0
    for ind, codes in by_ind.items():
        payload = {"wics_industry": ind, "wics_code": WICS_CODES.get(ind)}
        for i in range(0, len(codes), PATCH_CHUNK):
            chunk = codes[i:i + PATCH_CHUNK]
            try:
                sb.table("companies").update(payload).in_("code", chunk).execute()
                updated += len(chunk)
            except Exception as e:
                log.error(f"[WICS] 갱신 실패 ({ind} {i}~): {e}")
    return updated


def run(dry: bool = False) -> int:
    mapping = fetch_industry_map()

    groups = len(set(mapping.values()))
    if groups < MIN_GROUPS or len(mapping) < MIN_CODES:
        # 절반만 받은 상태로 쓰면 나머지가 옛 값으로 남아 기준이 섞인다 → 통째로 포기
        log.error(f"[WICS] 수집 불완전(업종 {groups}<{MIN_GROUPS} 또는 종목 {len(mapping)}<{MIN_CODES}) — 반영 중단")
        return 0

    n = apply_map(sb=get_supabase_client(), mapping=mapping, dry=dry)
    log.info(f"[WICS] {'조회만(dry)' if dry else '반영 완료'}: {n}개")
    return n


if __name__ == "__main__":
    print(f"완료: {run(dry='--dry' in sys.argv)}개")
