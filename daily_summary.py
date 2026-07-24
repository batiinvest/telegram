# -*- coding: utf-8 -*-
"""
daily_summary.py — 종목별 저녁 요약 생성
────────────────────────────────────────────────────────────────
그날의 공시(daily_disclosures) + 뉴스(daily_news)를 종목별로 묶어
요약을 만들고 daily_summaries에 저장한다. 프론트 종목 리포트 카드가 이걸 읽는다.

설계:
  - 규칙기반이 기본(비용 0). 대형/긴급 공시 종목만 Gemini 2줄 서술 = 하이브리드
  - 뉴스는 '이벤트 군집화' 후 대표 1건 + 매체 수로 노출.
    같은 실적 발표를 여러 매체가 받아쓴 기사가 하루치에 그대로 쌓이면 요약이 무의미해진다.
  - 뉴스 원천은 daily_news(발송 시점 적재)뿐. 네이버 재조회는 불가능
    (뉴스봇이 종일 폴링해 키 대부분이 일일 한도 소진).

실행: python3 daily_summary.py [YYYY-MM-DD]
"""
import re
import sys
import logging
import datetime

import config
from db_client import get_supabase_client
# 중복판정 기준은 뉴스봇과 단일 출처 공유 (news_main 모듈 레벨 함수)
from news_main import (normalize_title, title_sig, jaccard, extract_event_key,
                       DUP_SIM_THRESHOLD, DUP_EVENT_GATE)

try:
    import ai_analyst
except Exception:
    ai_analyst = None

KST = datetime.timezone(datetime.timedelta(hours=9))

# 대형/긴급 판단 카테고리 (jobs_collect._classify 라벨과 동일)
SIGNIFICANT_CATS = {
    '잠정실적', '주요사항', '증자/감자', '합병/분할', '자사주',
    '최대주주변동', '대량보유', '공정공시', '주요경영사항', '사채/전환',
}

MAX_NEWS = 5        # 카드에 노출할 뉴스 이벤트 수
MAX_DISC = 8        # 카드에 노출할 공시 수
MAX_AI_CALLS = 30   # 1회 실행당 Gemini 호출 상한 (무료 쿼터 보호)


# ══════════════════════════════════════════════════════════════
#  뉴스 이벤트 군집화
# ══════════════════════════════════════════════════════════════

def _num_sig(title: str) -> tuple:
    """
    제목의 특징적 수치를 강/약으로 분리.

    normalize_title은 중복판정을 위해 숫자를 지우지만, 하루치를 묶을 때는
    '9,752억' 같은 수치가 동일 발표를 가리키는 가장 강한 신호다.
      - strong(4자리+): '9752'·'3250' — 같은 종목·같은 날 우연 일치가 거의 없어
        이것만으로 동일 이벤트로 본다.
      - weak(3자리): '12.1%'→'121' 등 퍼센트는 우연 일치 가능 → 유사도 게이트 병행.
      - 3자리 미만('2분기' 등)은 잡음이라 버린다.
    """
    strong, weak = set(), set()
    for m in re.findall(r'\d[\d,\.]*', title or ''):
        t = m.replace(',', '').replace('.', '')
        if len(t) >= 4:
            strong.add(t)
        elif len(t) == 3:
            weak.add(t)
    return strong, weak


def cluster_news(company: str, arts: list) -> list:
    """
    같은 이벤트를 다룬 기사를 묶어 대표 1건 + 매체 수로 축약.

    뉴스봇의 is_duplicate()는 시간 TTL 기반 '지금 보낼지' 판정이라
    하루치를 한 번에 보여주는 요약에는 그대로 쓸 수 없다.
    """
    clusters = []
    for a in sorted(arts, key=lambda x: x['dt']):
        sig = title_sig(normalize_title(a['title']), company)
        ekey = extract_event_key(company, a['title'])
        strong, weak = _num_sig(a['title'])

        hit = None
        for c in clusters:
            j = jaccard(sig, c['sig'])
            if j >= DUP_SIM_THRESHOLD:                    # 제목 자체가 유사
                hit = c
                break
            if strong & c['strong']:                      # 같은 4자리+ 수치 = 같은 발표
                hit = c
                break
            if weak & c['weak'] and j >= DUP_EVENT_GATE:  # 퍼센트 일치 + 약한 유사
                hit = c
                break
            if ekey and ekey == c['ekey'] and j >= DUP_EVENT_GATE:
                hit = c
                break
        if hit:
            hit['arts'].append(a)
            hit['sig'] |= sig                             # 후속 변형 포착
            hit['strong'] |= strong
            hit['weak'] |= weak
        else:
            clusters.append({'sig': sig, 'ekey': ekey, 'strong': strong,
                             'weak': weak, 'arts': [a]})

    out = []
    for c in clusters:
        arts_c = c['arts']
        rep = max(arts_c, key=lambda x: len(x['title']))   # 정보량 최다 제목
        first = min(a['dt'] for a in arts_c)
        out.append({'title': rep['title'], 'link': rep['link'],
                    'time': first.strftime('%H:%M'), 'dt': first,
                    'sources': len(arts_c)})
    # 많이 보도된 이벤트일수록 그날의 핵심 → 커버리지 우선, 동수면 최신순.
    # (최초 보도시각순으로 정렬하면 일찍 터진 대형 이벤트가 뒤로 밀려 잘린다)
    out.sort(key=lambda x: (x['sources'], x['dt']), reverse=True)
    return out[:MAX_NEWS]


# ══════════════════════════════════════════════════════════════
#  하이브리드 AI 서술
# ══════════════════════════════════════════════════════════════

def ai_synthesis(corp_name: str, disc_titles: list, news_titles: list,
                 base_date: str) -> str | None:
    """대형/긴급 종목만 Gemini로 '오늘 한눈에' 2줄 서술."""
    if not ai_analyst or not getattr(ai_analyst, 'client', None):
        return None
    if not disc_titles and not news_titles:
        return None
    prompt = f"""당신은 주식 시장 정보를 정리하는 애널리스트입니다.
아래는 '{corp_name}'의 오늘({base_date}) 공시 제목과 뉴스 헤드라인입니다.
투자자가 오늘 이 종목에 무슨 일이 있었는지 2줄로 파악하도록 요약하세요.

[규칙]
- 2줄 이내, 쉬운 해요체.
- 호재/악재 단정, 매수/매도 권유, 목표주가·주가전망 표현 금지.
- 제공된 정보에 없는 내용은 추가하지 말 것.

[공시 제목]
{chr(10).join('- ' + t for t in disc_titles) or '- (없음)'}

[뉴스 헤드라인]
{chr(10).join('- ' + t for t in news_titles) or '- (없음)'}
"""
    try:
        resp = ai_analyst.client.models.generate_content(
            model=ai_analyst.AI_MODEL_ID, contents=prompt)
        return (resp.text or '').strip() or None
    except Exception as e:
        logging.warning(f"⚠️ [저녁요약] AI 실패({corp_name}): {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  데이터 로드
# ══════════════════════════════════════════════════════════════

def _load_disclosures(sb, base_date: str, monitored: set) -> dict:
    """base_date 공시 중 모니터링 종목만 corp_name 기준 그룹핑."""
    grouped = {}
    try:
        res = sb.table('daily_disclosures') \
                .select('corp_name,corp_code,report_nm,rcept_no,category,market_cap') \
                .eq('base_date', base_date).execute()
        for d in (res.data or []):
            if (d.get('corp_name') or '') in monitored:
                grouped.setdefault(d['corp_name'], []).append(d)
    except Exception as e:
        logging.error(f"❌ [저녁요약] 공시 조회 실패: {e}")
    return grouped


def _load_news(sb, base_date: str, monitored: set) -> dict:
    """base_date 뉴스를 corp_name 기준 그룹핑."""
    grouped = {}
    try:
        res = sb.table('daily_news') \
                .select('corp_name,title,link,source,published_at') \
                .eq('base_date', base_date).execute()
        for n in (res.data or []):
            name = n.get('corp_name') or ''
            if name not in monitored:
                continue
            try:
                dt = datetime.datetime.fromisoformat(
                    (n.get('published_at') or '').replace('Z', '+00:00')).astimezone(KST)
            except Exception:
                dt = datetime.datetime.now(KST)
            grouped.setdefault(name, []).append(
                {'title': n.get('title') or '', 'link': n.get('link') or '', 'dt': dt})
    except Exception as e:
        logging.error(f"❌ [저녁요약] 뉴스 조회 실패: {e}")
    return grouped


# ══════════════════════════════════════════════════════════════
#  생성
# ══════════════════════════════════════════════════════════════

def generate(base_date: str = None) -> int:
    """종목별 저녁 요약 생성 → daily_summaries 저장. 저장 건수 반환."""
    base_date = base_date or datetime.datetime.now(KST).date().isoformat()
    sb = get_supabase_client()
    if not sb:
        logging.error("❌ [저녁요약] Supabase 클라이언트 없음")
        return 0

    monitored = set(config.COMPANY_CODES.keys())
    if not monitored:
        logging.warning("⚠️ [저녁요약] 모니터링 종목 없음 — 중단")
        return 0

    disc_map = _load_disclosures(sb, base_date, monitored)
    news_map = _load_news(sb, base_date, monitored)
    targets = set(disc_map) | set(news_map)
    if not targets:
        logging.info(f"ℹ️ [저녁요약] {base_date} 활동 종목 없음")
        return 0

    # 1차: 규칙기반으로 전 종목 구성 (AI 없이)
    built = []
    for name in targets:
        discs = disc_map.get(name, [])
        news = cluster_news(name, news_map.get(name, []))
        cats = {d.get('category') for d in discs}
        is_major = bool(cats & SIGNIFICANT_CATS) or len(news) >= 3
        cap = max((d.get('market_cap') or 0) for d in discs) if discs else 0
        built.append({
            'name': name, 'discs': discs, 'news': news,
            'is_major': is_major, 'cap': cap,
        })

    # 2차: AI는 대형/긴급 종목에만, 그중에서도 상위 MAX_AI_CALLS개만 (쿼터 보호)
    majors = sorted([b for b in built if b['is_major']],
                    key=lambda b: (b['cap'], len(b['discs']) + len(b['news'])),
                    reverse=True)[:MAX_AI_CALLS]
    ai_done = 0
    for b in majors:
        b['ai'] = ai_synthesis(
            b['name'],
            [(d.get('report_nm') or '').strip() for d in b['discs']],
            [n['title'] for n in b['news']], base_date)
        if b['ai']:
            ai_done += 1

    # 3차: 저장
    rows = []
    for b in built:
        rows.append({
            'base_date':      base_date,
            'corp_name':      b['name'],
            'stock_code':     config.COMPANY_CODES.get(b['name']),
            'industry':       config.COMPANY_TO_INDUSTRY.get(b['name']),
            'disclosure_cnt': len(b['discs']),
            'news_cnt':       len(b['news']),
            'is_major':       b['is_major'],
            'ai_summary':     b.get('ai'),
            'items': {
                'disclosures': [{
                    'category':  d.get('category') or '기타',
                    # DART report_nm은 후행 공백이 다수 포함돼 옴
                    'report_nm': (d.get('report_nm') or '').strip(),
                    'rcept_no':  d.get('rcept_no'),
                } for d in b['discs'][:MAX_DISC]],
                'news': [{
                    'title':   n['title'],
                    'link':    n['link'],
                    'time':    n['time'],
                    'sources': n['sources'],
                } for n in b['news']],
            },
            'updated_at': datetime.datetime.now(KST).isoformat(),
        })

    saved = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        try:
            sb.table('daily_summaries').upsert(
                chunk, on_conflict='base_date,corp_name').execute()
            saved += len(chunk)
        except Exception as e:
            logging.error(f"❌ [저녁요약] 저장 실패(청크 {i // 100}): {e}")

    logging.info(f"🌙 [저녁요약] {base_date} — {saved}개 종목 저장 "
                 f"(대형 {len(majors)}, AI서술 {ai_done})")
    return saved


def run():
    """스케줄러 진입점."""
    return generate()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"저장 완료: {generate(arg)}개 종목")
