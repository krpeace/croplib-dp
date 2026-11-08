-- 목적: KREI 농업관측 수집기 1단계 스키마(krei, krei_api) 및 비공개 Storage 버킷(krei-raw) 생성.
--   1. krei.item: KREI 품목분류(reportItem) 사전 (3단계, 사라진 코드는 is_listed = false 로 보존)
--   2. krei.report: 발행물(월보·순보·분기보·속보) 메타데이터 및 글자 좌표(pages.json.gz) Storage 경로
--   3. krei_api.reports: 외부 공동활용용 공개 뷰 (fetch_status = 'fetched' 만, security_invoker = true)
--   4. storage.buckets 'krei-raw': 목록 XML 원문과 글자 좌표(pages.json.gz)를 보관하는 비공개 버킷
--
-- 실행 방법:
--   1. Supabase 대시보드 → SQL Editor 에 이 파일 내용을 모두 붙여 넣고 Run 을 누른다.
--      여러 번 실행해도 오류가 나지 않는다(멱등).
--   2. Supabase 대시보드 → Settings → API → Exposed schemas 에 `krei`, `krei_api` 를 추가한다.
--      (수집기는 Content-Profile / Accept-Profile: krei 헤더로 읽고 쓰며, 외부 사용처 안내는 krei_api 만 한다.)
--
-- 결정 기록: docs/decisions/0019-krei-collector.md

-- ---------------------------------------------------------------------------
-- 1. 스키마 및 기본 표 (krei.item, krei.report)
-- ---------------------------------------------------------------------------
create schema if not exists krei;

create table if not exists krei.item (        -- reportItem 그대로
  ovr_item_code      text primary key,        -- 'OVR0000000005'
  prt_ovr_item_code  text,                    -- 최상위는 null
  krei_item_code     text,                    -- '05' (문자열, 앞자리 0 유지)
  prt_krei_item_code text,
  item_name          text not null,
  prt_item_name      text,
  level              smallint not null,       -- 1 원예·축산·곡물 / 2 부류 / 3 품목
  order_num          int,
  img_link           text,
  is_listed          boolean not null default true,  -- 최근 응답에 있었는지(사라진 코드는 false, 지우지 않음)
  updated_at         timestamptz not null default now()
);

create table if not exists krei.report (      -- 발행물 1건 = 1행
  file_id            int primary key,         -- pdf_link 안 파일번호 (8778)
  api                text not null check (api in ('monthly','news')),  -- 어느 API에서 왔나
  report_type        text not null,           -- title 원문: 월보/순보/분기보/…
  yyyy               smallint not null,
  mm                 smallint not null,
  ovr_item_code      text not null references krei.item,   -- 부류 코드
  prt_ovr_item_code  text,
  item_name          text not null,           -- '과일'
  pdf_link           text not null,
  reg_at             timestamptz,             -- reg_date를 KST로 해석
  mod_at             timestamptz,
  description        text,
  first_seen_at      timestamptz not null default now(),
  pdf_sha256         text,
  pdf_bytes          int,
  pages              smallint,
  issue_no           int,                     -- 통권 (1쪽에서 추출, 없으면 null)
  pub_date           date,                    -- 발행일 (1쪽에서 추출, 없으면 null)
  text_path          text,                    -- Storage 경로
  fetch_status       text not null default 'listed'
                     check (fetch_status in ('listed','fetched','failed')),
  fetch_error        text,
  parse_status       text not null default 'pending',  -- 다음 단계(#4~)에서 사용
  updated_at         timestamptz not null default now()
);

create index if not exists report_yyyy_mm_idx on krei.report (yyyy, mm);
create index if not exists report_ovr_item_code_yyyy_mm_idx on krei.report (ovr_item_code, yyyy, mm);

-- ---------------------------------------------------------------------------
-- 2. 권한 및 RLS: 누구나 읽기(SELECT)만 허용, 쓰기는 서버 키(service_role)만
-- ---------------------------------------------------------------------------
grant usage on schema krei to anon, authenticated, service_role;
grant all on all tables in schema krei to service_role;
grant all on all sequences in schema krei to service_role;
alter default privileges in schema krei grant all on tables to service_role;
alter default privileges in schema krei grant all on sequences to service_role;
alter default privileges in schema krei grant select on tables to anon, authenticated;

alter table krei.item enable row level security;
revoke all on krei.item from anon, authenticated;
grant select on krei.item to anon, authenticated;
drop policy if exists "krei.item 누구나 읽기" on krei.item;
create policy "krei.item 누구나 읽기"
  on krei.item
  for select
  to anon, authenticated
  using (true);

alter table krei.report enable row level security;
revoke all on krei.report from anon, authenticated;
grant select on krei.report to anon, authenticated;
drop policy if exists "krei.report 누구나 읽기" on krei.report;
create policy "krei.report 누구나 읽기"
  on krei.report
  for select
  to anon, authenticated
  using (true);

-- ---------------------------------------------------------------------------
-- 3. 공개 뷰 스키마 (krei_api.reports)
--    report + item 이름, fetch_status = 'fetched' 만, security_invoker = true
-- ---------------------------------------------------------------------------
create schema if not exists krei_api;

create or replace view krei_api.reports
with (security_invoker = true) as
select
  r.file_id,
  r.api,
  r.report_type,
  r.yyyy,
  r.mm,
  r.ovr_item_code,
  coalesce(i.item_name, r.item_name) as item_name,
  coalesce(i.prt_ovr_item_code, r.prt_ovr_item_code) as prt_ovr_item_code,
  i.prt_item_name,
  i.krei_item_code,
  i.prt_krei_item_code,
  i.level as item_level,
  r.pdf_link,
  r.reg_at,
  r.mod_at,
  r.description,
  r.first_seen_at,
  r.pdf_sha256,
  r.pdf_bytes,
  r.pages,
  r.issue_no,
  r.pub_date,
  r.text_path,
  r.fetch_status,
  r.parse_status,
  r.updated_at
from krei.report r
left join krei.item i on i.ovr_item_code = r.ovr_item_code
where r.fetch_status = 'fetched';

grant usage on schema krei_api to anon, authenticated, service_role;
revoke all on krei_api.reports from anon, authenticated;
grant select on krei_api.reports to anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 4. Storage 버킷 krei-raw (비공개: 쓰기·읽기 모두 service_role)
--    Supabase 가 아닌 PostgreSQL(테스트용)에는 storage 스키마가 없어서 건너뛴다.
-- ---------------------------------------------------------------------------
do $$
begin
  if to_regclass('storage.buckets') is not null then
    insert into storage.buckets (id, name, public)
      values ('krei-raw', 'krei-raw', false)
      on conflict (id) do update set public = false;
  end if;
end
$$;
