-- market_data (stock_code, base_date) unique 인덱스 3중복 정리 — 1개만 유지
-- 배경: 2026-07-09 NULL 장애 수습 과정에서 동일 컬럼 unique 인덱스가 3개 생김.
-- 유지 대상: market_data_stock_date_uidx (현행 upsert on_conflict가 사용하는 인덱스)
-- Supabase SQL 에디터에서 실행.

-- 0) 실행 전 현황 확인 (참고용):
--   select indexname, indexdef from pg_indexes
--   where schemaname='public' and tablename='market_data' and indexdef ilike '%unique%';

do $$
declare
  r record;
  dropped int := 0;
begin
  -- 안전장치: 유지 대상 인덱스가 없으면 아무것도 지우지 않고 중단
  if not exists (
    select 1 from pg_indexes
    where schemaname = 'public' and tablename = 'market_data'
      and indexname = 'market_data_stock_date_uidx'
  ) then
    raise exception 'market_data_stock_date_uidx 없음 — 유지 대상부터 확인 필요 (아무것도 삭제 안 함)';
  end if;

  for r in
    select i.indexrelid::regclass::text as idx,
           (select con.conname from pg_constraint con
             where con.conindid = i.indexrelid) as con
    from pg_index i
    where i.indrelid = 'public.market_data'::regclass
      and i.indisunique
      and not i.indisprimary          -- PK는 절대 건드리지 않음
      and array(
            select a.attname::text
            from unnest(i.indkey) with ordinality k(attnum, ord)
            join pg_attribute a
              on a.attrelid = i.indrelid and a.attnum = k.attnum
            order by k.ord
          ) in (array['stock_code','base_date'], array['base_date','stock_code'])
      and i.indexrelid::regclass::text <> 'market_data_stock_date_uidx'
  loop
    if r.con is not null then
      -- unique 제약이 백업하는 인덱스는 제약을 지워야 함 (인덱스도 함께 제거됨)
      execute format('alter table public.market_data drop constraint %I', r.con);
    else
      execute format('drop index public.%I', r.idx);
    end if;
    dropped := dropped + 1;
    raise notice '제거: % (제약: %)', r.idx, coalesce(r.con, '없음');
  end loop;

  raise notice '완료 — %개 제거, market_data_stock_date_uidx 유지', dropped;
end $$;

-- 1) 실행 후 확인: unique 인덱스가 market_data_stock_date_uidx 1개만 남아야 함
select indexname from pg_indexes
where schemaname='public' and tablename='market_data' and indexdef ilike '%unique%';
