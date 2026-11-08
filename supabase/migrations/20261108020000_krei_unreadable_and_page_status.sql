-- 목적: KREI 판독 불가(그림 PDF) 쪽 단위 판정 열(no_text_pages), text_status 'partial' 허용,
--       그리고 외부(CropMon 등)에서 판독 불가 원문 안내에 쓸 공개 뷰(krei_api.unreadable) 추가.
--   - no_text_pages: 쪽당 글자 200자 미만인 쪽 수 (smallint)
--   - text_status: 전부 글자 없으면 'no_text', 절반 이상이면 'partial', 나머지 'ok'
--   - krei_api.unreadable: 대상 범위(원예 4부류 + 쌀·콩·감자) 중 'no_text'·'partial'인 건의
--     부류·월호·파일번호·pdf_link 목록
--
-- 실행 방법:
--   Supabase 대시보드 → SQL Editor 에 이 파일 내용을 붙여 넣고 Run 을 누른다 (멱등).
--
-- 결정 기록: docs/decisions/0019-krei-collector.md

alter table krei.report
  add column if not exists no_text_pages smallint;

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
    where c.conrelid = 'krei.report'::regclass
      and c.contype = 'c'
      and a.attname = 'text_status'
  loop
    execute format('alter table krei.report drop constraint %I', con.conname);
  end loop;
end
$$;

alter table krei.report
  add constraint report_text_status_check
  check (text_status in ('ok', 'partial', 'no_text'));

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
  r.updated_at,
  r.text_chars,
  r.text_status,
  r.no_text_pages
from krei.report r
left join krei.item i on i.ovr_item_code = r.ovr_item_code
where r.fetch_status = 'fetched';

create or replace view krei_api.unreadable
with (security_invoker = true) as
select
  r.file_id,
  r.ovr_item_code,
  coalesce(i.item_name, r.item_name) as item_name,
  r.yyyy,
  r.mm,
  r.report_type,
  r.pages,
  r.no_text_pages,
  r.text_chars,
  r.text_status,
  r.pdf_link
from krei.report r
left join krei.item i on i.ovr_item_code = r.ovr_item_code
where r.fetch_status = 'fetched'
  and r.text_status in ('no_text', 'partial')
  and r.ovr_item_code in (
    'OVR0000000013', -- 과일
    'OVR0000000031', -- 과채
    'OVR0000000008', -- 엽근채소
    'OVR0000000036', -- 양념채소
    'OVR0000000070', -- 쌀
    'OVR0000000072', -- 콩
    'OVR0000000021'  -- 감자
  );

grant usage on schema krei_api to anon, authenticated, service_role;
revoke all on krei_api.reports from anon, authenticated;
grant select on krei_api.reports to anon, authenticated, service_role;
revoke all on krei_api.unreadable from anon, authenticated;
grant select on krei_api.unreadable to anon, authenticated, service_role;
