"""
supabase_bridge.py
──────────────────
봇 ↔ Supabase 연동 모듈 (기존 코드 최소 수정)

설치:
    pip install supabase

사용법:
    from supabase_bridge import bridge

    # 설정 읽기
    keywords = bridge.get_ai_keywords()

    # 공지 기록 저장
    bridge.log_notice(target="@Kbiochat", content="...", sent=1, ok=1)

    # 봇 heartbeat 업데이트
    bridge.heartbeat("dart_bot")
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional, List

# .env 파일 로드 (환경변수가 시스템에 없을 때 폴백)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from supabase import create_client, Client
    _SUPABASE_AVAILABLE = True
except ImportError:
    logging.warning("⚠️ supabase 패키지가 없습니다. pip install supabase")
    _SUPABASE_AVAILABLE = False

# ── 환경변수 ──
SB_URL = os.getenv("SB_URL", "")
SB_KEY = os.getenv("SB_SERVICE_KEY", "")  # service_role 키


class SupabaseBridge:
    def __init__(self):
        self._client: Optional[Client] = None
        self._config_cache = {}
        self._cache_ts = None
        self._cache_ttl = 300  # 5분 캐시
        self._last_reload_flag      = None  # 마지막으로 확인한 reload_flag 값
        self._last_disclosure_flag  = None  # 마지막으로 확인한 run_disclosure_flag 값

    def _get_client(self) -> Optional[Client]:
        if not _SUPABASE_AVAILABLE:
            return None
        if not SB_URL or not SB_KEY:
            return None
        if not self._client:
            try:
                self._client = create_client(SB_URL, SB_KEY)
            except Exception as e:
                logging.error(f"[Bridge] Supabase 연결 실패: {e}")
                return None
        return self._client

    # ══════════════════════════════════════════
    #  설정 읽기 (app_config 테이블)
    # ══════════════════════════════════════════
    def _load_config(self):
        """app_config 전체를 딕셔너리로 로드 (5분 캐시)"""
        now = datetime.now(timezone.utc).timestamp()
        if self._config_cache and self._cache_ts and (now - self._cache_ts) < self._cache_ttl:
            return self._config_cache

        client = self._get_client()
        if not client:
            return self._config_cache  # 캐시라도 반환

        try:
            res = client.table("app_config").select("key,value").execute()
            self._config_cache = {row["key"]: row["value"] for row in (res.data or [])}
            self._cache_ts = now
        except Exception as e:
            logging.error(f"[Bridge] config 로드 실패: {e}")

        return self._config_cache

    def get_config(self, key: str, default=None):
        """설정값 하나 읽기"""
        return self._load_config().get(key, default)

    def get_ai_keywords(self) -> List[str]:
        """AI 트리거 키워드 목록 (쉼표 구분)"""
        raw = self.get_config("ai_trigger_keywords", "")
        if not raw:
            return []
        return [k.strip() for k in raw.split(",") if k.strip()]

    def get_global_keywords(self) -> List[str]:
        """전체 공시 중요 키워드 목록"""
        raw = self.get_config("global_important_keywords", "")
        if not raw:
            return []
        return [k.strip() for k in raw.split(",") if k.strip()]

    def is_schedule_enabled(self, job_name: str) -> bool:
        """스케줄 ON/OFF 확인 (예: 'schedule_lunch', 'schedule_closing')"""
        val = self.get_config(f"schedule_{job_name}", "1")
        return val not in ("0", "false", "False", "off")

    # ══════════════════════════════════════════
    #  공지 발송 기록
    # ══════════════════════════════════════════
    def log_notice(self, target: str, content: str, sent_count: int = 1, ok_count: int = 1):
        """공시/뉴스/브리핑 발송 기록을 notice_history에 저장"""
        client = self._get_client()
        if not client:
            return

        try:
            client.table("notice_history").insert({
                "target":     target,
                "content":    content[:500],   # 너무 길면 자름
                "sent_count": sent_count,
                "ok_count":   ok_count,
            }).execute()
        except Exception as e:
            logging.debug(f"[Bridge] notice 기록 실패 (무시): {e}")

    # ══════════════════════════════════════════
    #  봇 상태 Heartbeat
    # ══════════════════════════════════════════
    def heartbeat(self, bot_name: str):
        """봇 생존 신호를 app_config에 업데이트"""
        client = self._get_client()
        if not client:
            return

        now_str = datetime.now(timezone.utc).isoformat()
        key = f"heartbeat_{bot_name}"

        try:
            # upsert: 없으면 insert, 있으면 update
            client.table("app_config").upsert({
                "key":         key,
                "value":       now_str,
                "description": f"{bot_name} 마지막 생존 신호",
            }, on_conflict="key").execute()
        except Exception as e:
            logging.debug(f"[Bridge] heartbeat 실패 (무시): {e}")

    # ══════════════════════════════════════════
    #  채팅방 목록 읽기 (rooms 테이블)
    # ══════════════════════════════════════════
    def get_company_chat_ids(self) -> dict:
        """
        rooms 테이블에서 종목명 → chat_id 딕셔너리 반환 (room_type != 'industry').
        종목명 변경에 대비해 stock_code → chat_id 딕셔너리도 window._chatByCode에 저장.

        반환: {corp_name: chat_id}  ← 기존 호환
        부산물: bridge.chat_ids_by_code = {stock_code: chat_id}  ← 코드 기반 fallback용
        """
        client = self._get_client()
        if not client:
            return {}
        try:
            res = client.table("rooms").select("name,code,chat_id,room_type").execute()
            by_name = {}
            by_code = {}
            for row in (res.data or []):
                if not row.get("chat_id") or row.get("room_type") == "industry":
                    continue
                if row.get("name"):
                    by_name[row["name"]] = row["chat_id"]
                # code 컬럼이 있으면 코드 기준도 저장 (종목명 변경 시 fallback)
                if row.get("code"):
                    code = str(row["code"]).split(".")[0]  # '.KS' 등 suffix 제거
                    by_code[code] = row["chat_id"]
            # 코드 기준 매핑을 인스턴스 속성으로 저장 (config.py에서 참조 가능)
            self.chat_ids_by_code = by_code
            return by_name
        except Exception as e:
            logging.error(f"[Bridge] rooms(company) 로드 실패: {e}")
            return {}

    def get_industry_chat_ids(self) -> dict:
        """rooms 테이블에서 산업명 → chat_id (room_type='industry')"""
        client = self._get_client()
        if not client:
            return {}
        try:
            res = client.table("rooms").select("cat,chat_id,room_type").eq("room_type", "industry").execute()
            return {
                row["cat"]: row["chat_id"]
                for row in (res.data or [])
                if row.get("chat_id") and row.get("cat")
            }
        except Exception as e:
            logging.error(f"[Bridge] rooms(industry) 로드 실패: {e}")
            return {}

    def get_companies(self, level: str = None) -> list:
        """
        companies 테이블에서 종목 로드 (페이징 적용).

        level 파라미터:
          None        → is_monitored=True 전체 (full + news)
          'full'      → 채팅방 + 뉴스/공시 모니터링
          'news'      → 뉴스/공시 모니터링만
          'monitored' → full + news (is_monitored=True)
          'all'       → 전체 상장사 포함
        """
        client = self._get_client()
        if not client:
            return []

        try:
            all_rows = []
            page = 0
            page_size = 1000
            while True:
                q = client.table("companies").select(
                    "name, code, corp_code, market, industry, sub_industry, "
                    "keywords, "
                    "active, is_monitored, monitoring_level"
                )

                if level == 'all':
                    q = q.eq("active", True)
                elif level in ('full', 'news', 'data'):
                    q = q.eq("monitoring_level", level)
                else:
                    # 기본: 뉴스/공시 모니터링 종목 (full + news)
                    q = q.eq("is_monitored", True)

                res = q.order("name").range(
                    page * page_size, (page + 1) * page_size - 1
                ).execute()

                rows = res.data or []
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                page += 1

            return all_rows
        except Exception as e:
            logging.error(f"[Bridge] companies 로드 실패: {e}")
            return []

    def get_industry_companies(self) -> list:
        """
        INDUSTRY_HIERARCHY / THEME_MAP 구성용.
        industry가 있는 전체 활성 종목을 가져옵니다 (monitoring_level 무관).
        페이징 적용.
        """
        client = self._get_client()
        if not client:
            return []

        try:
            all_rows = []
            page = 0
            page_size = 1000
            while True:
                res = client.table("companies").select(
                    "name, industry, sub_industry, active, monitoring_level"
                ).eq("active", True)\
                 .neq("industry", "")\
                 .not_.is_("industry", None)\
                 .order("name")\
                 .range(page * page_size, (page + 1) * page_size - 1)\
                 .execute()

                rows = res.data or []
                # 파이썬 단에서 industry 없는 행 한번 더 필터 (방어)
                rows = [r for r in rows if r.get("industry")]
                all_rows.extend(rows)
                if len(res.data or []) < page_size:
                    break
                page += 1

            logging.info(f"[Bridge] industry_companies: {len(all_rows)}개 로드")
            return all_rows
        except Exception as e:
            logging.error(f"[Bridge] industry_companies 로드 실패: {e}")
            # 폴백: active 전체 로드 후 파이썬에서 필터
            try:
                all_rows = []
                page = 0
                while True:
                    res = client.table("companies").select(
                        "name, industry, sub_industry, active, monitoring_level"
                    ).eq("active", True).order("name").range(
                        page * 1000, (page + 1) * 1000 - 1
                    ).execute()
                    rows = [r for r in (res.data or []) if r.get("industry")]
                    all_rows.extend(rows)
                    if len(res.data or []) < 1000:
                        break
                    page += 1
                logging.info(f"[Bridge] industry_companies 폴백: {len(all_rows)}개")
                return all_rows
            except Exception as e2:
                logging.error(f"[Bridge] industry_companies 폴백도 실패: {e2}")
                return []

    def get_news_search_terms(self) -> dict:
        """
        app_config에서 산업별 뉴스 검색어 로드.
        반환: { '반도체': ['HBM', 'CXL', ...], '바이오': [...], ... }
        """
        client = self._get_client()
        if not client:
            return {}
        try:
            res = client.table("app_config").select("key,value")                 .like("key", "news_terms_%").execute()
            result = {}
            for row in (res.data or []):
                industry = row["key"].replace("news_terms_", "")
                terms = [t.strip() for t in row["value"].split(",") if t.strip()]
                if terms:
                    result[industry] = terms
            return result
        except Exception as e:
            logging.error(f"[Bridge] news_terms 로드 실패: {e}")
            return {}

    def request_reload(self, reason: str = "manual"):
        """대시보드에서 봇 재로드 요청 신호 전송"""
        client = self._get_client()
        if not client:
            return False
        try:
            import time
            client.table("app_config").upsert({
                "key":         "reload_flag",
                "value":       str(time.time()),
                "description": f"재로드 요청: {reason}",
            }, on_conflict="key").execute()
            logging.info(f"[Bridge] 재로드 요청 전송: {reason}")
            return True
        except Exception as e:
            logging.error(f"[Bridge] 재로드 요청 실패: {e}")
            return False

    def check_reload_flag(self) -> bool:
        """
        봇에서 호출 — reload_flag가 마지막 체크 이후 변경됐으면 True 반환.
        True 반환 시 종목 데이터를 다시 로드해야 합니다.
        """
        client = self._get_client()
        if not client:
            return False
        try:
            res = client.table("app_config").select("value").eq("key", "reload_flag").execute()
            if not res.data:
                return False
            flag_val = res.data[0]["value"]
            if flag_val != self._last_reload_flag:
                self._last_reload_flag = flag_val
                logging.info(f"[Bridge] 재로드 신호 감지 — 종목 데이터 갱신")
                return True
            return False
        except Exception as e:
            logging.debug(f"[Bridge] reload_flag 체크 실패: {e}")
            return False

    def check_disclosure_flag(self) -> bool:
        """
        봇에서 호출 — run_disclosure_flag가 마지막 체크 이후 변경됐으면 True 반환.
        True 반환 시 공시 수집 job을 즉시 실행해야 합니다.
        """
        client = self._get_client()
        if not client:
            return False
        try:
            res = client.table("app_config").select("value").eq("key", "run_disclosure_flag").execute()
            if not res.data:
                return False
            flag_val = res.data[0]["value"]
            if flag_val != self._last_disclosure_flag:
                self._last_disclosure_flag = flag_val
                logging.info(f"[Bridge] 공시수집 수동 트리거 감지")
                return True
            return False
        except Exception as e:
            logging.debug(f"[Bridge] disclosure_flag 체크 실패: {e}")
            return False

    def update_industry_map_cache(self):
        """
        모니터링 종목의 산업 매핑을 app_config 에 JSON 으로 스냅샷 저장.
        프론트엔드 getIndustryMap()이 app_config 에서 바로 읽어 DB 조회 생략 가능.

        저장 키:
          industry_map     : JSON {code: industry}
          sub_industry_map : JSON {code: sub_industry}
        """
        import json
        client = self._get_client()
        if not client:
            return
        try:
            from db_utils import fetch_all_pages
            rows = fetch_all_pages(
                client.table('companies')
                      .select('code,industry,sub_industry')
                      .eq('is_monitored', True)
            )
            ind_map = {}
            sub_map = {}
            for r in rows:
                code = (r.get('code') or '').replace('.KS', '').replace('.KQ', '')
                if not code:
                    continue
                if r.get('industry'):
                    ind_map[code] = r['industry']
                if r.get('sub_industry'):
                    sub_map[code] = r['sub_industry']

            client.table('app_config').upsert([
                {'key': 'industry_map',
                 'value': json.dumps(ind_map, ensure_ascii=False),
                 'description': f'모니터링 종목 산업 매핑 캐시 ({len(ind_map)}개)'},
                {'key': 'sub_industry_map',
                 'value': json.dumps(sub_map, ensure_ascii=False),
                 'description': f'모니터링 종목 세부섹터 매핑 캐시 ({len(sub_map)}개)'},
            ], on_conflict='key').execute()

            logging.info(f"[Bridge] industry_map 캐시 갱신 완료: {len(ind_map)}개 종목")
        except Exception as e:
            logging.warning(f"[Bridge] industry_map 캐시 갱신 실패 (무시): {e}")

    def seed_industry_rooms(self, industry_chat_ids: dict):
        """
        rooms 테이블에 없는 산업 채팅방을 자동 삽입.
        이미 존재하는 산업(cat)은 건드리지 않음.
        """
        client = self._get_client()
        if not client:
            return
        try:
            res = client.table("rooms").select("cat").eq("room_type", "industry").execute()
            existing = {r["cat"] for r in (res.data or []) if r.get("cat")}
            to_insert = [
                {
                    "name": ind,
                    "cat": ind,
                    "chat_id": chat_id,
                    "room_type": "industry",
                    "members": 0,
                    "status": "open",
                    "max_members": 5000,
                }
                for ind, chat_id in industry_chat_ids.items()
                if ind not in existing
            ]
            if to_insert:
                client.table("rooms").insert(to_insert).execute()
                logging.info(f"[Bridge] 산업 채팅방 {len(to_insert)}개 rooms 테이블에 시드 완료")
        except Exception as e:
            logging.warning(f"[Bridge] seed_industry_rooms 실패 (무시): {e}")

    def seed_defaults(self, defaults: dict):
        """
        DB에 없는 키만 INSERT (이미 있는 키는 건드리지 않음).
        봇 최초 실행 시 하드코딩 기본값을 DB에 씁니다.
        defaults: { 'key': 'value', ... }
        """
        client = self._get_client()
        if not client:
            return
        try:
            keys = list(defaults.keys())
            res = client.table("app_config").select("key").in_("key", keys).execute()
            existing = {r["key"] for r in (res.data or [])}
            to_insert = [
                {"key": k, "value": v, "description": "봇 기본값 (자동 시드)"}
                for k, v in defaults.items()
                if k not in existing
            ]
            if to_insert:
                client.table("app_config").insert(to_insert).execute()
                logging.info(f"[Bridge] 기본값 시드 완료: {[r['key'] for r in to_insert]}")
        except Exception as e:
            logging.warning(f"[Bridge] seed_defaults 실패 (무시): {e}")

    def invalidate_config_cache(self):
        """캐시 강제 초기화 (설정 변경 후 즉시 반영 시)"""
        self._config_cache = {}
        self._cache_ts = None

    def fetch_all_pages(self, query_builder, page_size: int = 1000) -> list:
        """
        Supabase 페이지네이션 공통 헬퍼.
        query_builder: sb.table(...).select(...).eq(...) 등 range() 전까지의 쿼리 객체
        반환: 전체 rows 리스트

        사용 예:
            rows = bridge.fetch_all_pages(
                client.table('companies').select('name,code').eq('active', True)
            )
        """
        client = self._get_client()
        if not client:
            return []
        all_rows = []
        page = 0
        while True:
            try:
                res = query_builder.range(
                    page * page_size, (page + 1) * page_size - 1
                ).execute()
                rows = res.data or []
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                page += 1
            except Exception as e:
                logging.error(f"[Bridge] fetch_all_pages 오류 (page={page}): {e}")
                break
        return all_rows


# 싱글톤 인스턴스
bridge = SupabaseBridge()
