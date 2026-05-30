"""
db_utils.py — 공통 DB 유틸
────────────────────────────
supabase_bridge.SupabaseBridge.fetch_all_pages()와 동일한 로직을
bridge 의존 없이 독립적으로 사용할 수 있는 모듈.

사용법:
    from db_utils import fetch_all_pages

    rows = fetch_all_pages(
        sb.from_('market_data')
          .select('base_date,stock_code,price')
          .eq('stock_code', '005930')
          .order('base_date', desc=False)
    )
"""

import logging

log = logging.getLogger(__name__)


def fetch_all_pages(query_builder, page_size: int = 1000) -> list:
    """
    Supabase 페이지네이션 공통 헬퍼.

    Args:
        query_builder : range() 호출 전까지 빌드된 Supabase 쿼리 객체
        page_size     : 한 페이지당 row 수 (기본 1000 — Supabase 기본 limit 동일)

    Returns:
        전체 rows 리스트

    주의:
        query_builder 객체는 재사용이 안 되는 경우가 있으므로
        필요하면 람다/팩토리 방식으로 전달.
    """
    all_rows: list = []
    page: int = 0
    while True:
        try:
            res = query_builder.range(
                page * page_size,
                (page + 1) * page_size - 1,
            ).execute()
            rows = res.data or []
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
            page += 1
        except Exception as e:
            log.error(f"[db_utils] fetch_all_pages 오류 (page={page}): {e}")
            break
    return all_rows
