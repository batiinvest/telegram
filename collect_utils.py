"""
collect_utils.py — 수집 스크립트 공통 유틸
────────────────────────────────────────────
collect_*.py / backfill_*.py 에서 반복되는 패턴을 통합합니다.

포함:
  - fetch_all_pages()  : 인라인 while True: .range() 패턴 통합 (db_utils 위임)
  - batch_upsert()     : for i in range(0, len(records), N): 패턴 통합
  - require_env()      : 필수 환경변수 검증
"""

import logging
import os

log = logging.getLogger(__name__)


# ── 페이지네이션 ──────────────────────────────────────────────────────────────

def fetch_all_pages(query_builder, page_size: int = 1000) -> list:
    """
    Supabase 전체 페이지 조회.
    db_utils.fetch_all_pages() 에 위임 (단일 진입점).

    사용법:
        from collect_utils import fetch_all_pages
        rows = fetch_all_pages(sb.from_('market_data').select('*').eq('col', val))
    """
    from db_utils import fetch_all_pages as _fap
    return _fap(query_builder, page_size)


# ── 배치 upsert ───────────────────────────────────────────────────────────────

def batch_upsert(sb, table: str, records: list,
                 conflict_col: str, chunk: int = 100) -> int:
    """
    레코드 리스트를 청크 단위로 upsert.
    collect_*.py 전반의 `for i in range(0, len(records), N):` 패턴 통합.

    Returns:
        성공 upsert 건수
    """
    if not records:
        return 0
    total = 0
    for i in range(0, len(records), chunk):
        batch = records[i:i + chunk]
        try:
            sb.table(table).upsert(batch, on_conflict=conflict_col).execute()
            total += len(batch)
        except Exception as e:
            log.error(f"[collect_utils] batch_upsert 오류 ({table} chunk {i}): {e}")
    return total


# ── 환경변수 검증 ─────────────────────────────────────────────────────────────

def require_env(*names: str) -> dict:
    """
    필수 환경변수가 모두 설정됐는지 확인.
    없으면 RuntimeError 발생.

    Returns:
        {name: value} dict
    """
    result = {}
    missing = []
    for name in names:
        val = os.getenv(name, "")
        if not val:
            missing.append(name)
        else:
            result[name] = val
    if missing:
        raise RuntimeError(f"필수 환경변수 미설정: {', '.join(missing)}")
    return result
