-- 목적: krei.report 에 추출 글자 수(text_chars)와 글자 유무 판정(text_status) 열 추가.
--   - text_chars: pages.json.gz 에서 추출된 전체 글자 수 (단어 글자 수 합)
--   - text_status: 쪽당 평균 50자 이상이면 'ok', 50자 미만(윤곽선·통이미지 PDF)이면 'no_text'
--
-- 실행 방법:
--   Supabase 대시보드 → SQL Editor 에 이 파일 내용을 붙여 넣고 Run 을 누른다 (멱등).
--
-- 결정 기록: docs/decisions/0019-krei-collector.md

alter table krei.report
  add column if not exists text_chars int,
  add column if not exists text_status text
    check (text_status in ('ok', 'no_text'));

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
  r.text_status
from krei.report r
left join krei.item i on i.ovr_item_code = r.ovr_item_code
where r.fetch_status = 'fetched';
