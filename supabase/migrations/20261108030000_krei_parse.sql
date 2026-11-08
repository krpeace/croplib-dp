-- 목적: KREI 농업관측 2단계 스키마 — 섹션 헤드라인, 표 원형, 정형 숫자(observation), 생육 분포(survey_dist),
--       그리고 외부 공동활용용 공개 뷰(latest_outlook, outlook_history, headlines, growth_survey).
--
-- 대상 표:
--   1. krei.section_text: 섹션 헤드라인과 근거 문장(bullets)
--   2. krei.table_raw: 모든 표의 원형 (머리글 jsonb, 행 jsonb, 주, 자료)
--   3. krei.observation: 핵심 표 4종에서 검사를 통과한 정형 숫자 (long format)
--   4. krei.survey_dist: 생육·품질 분포 (좋음/비슷/나쁨)
--   5. krei.report 열 추가: parser_version text, parsed_at timestamptz
--
-- 결정 기록: docs/decisions/0023-krei-parse.md

-- ---------------------------------------------------------------------------
-- 1. 기본 표 생성
-- ---------------------------------------------------------------------------

-- 섹션 헤드라인과 근거 문장
create table if not exists krei.section_text (
  id          bigserial primary key,
  file_id     int not null references krei.report on delete cascade,
  page        smallint not null,
  item_code   text references krei.item,     -- 품목(level 3) 코드, 못 정하면 null
  item_label  text,                          -- 원문 품목 표기 ("사과", "노지감귤" …)
  section     text not null,                 -- 가격동향/생육동향/생산전망/저장의향/출하전망/가격전망/기타
  headline    text not null,
  direction   text,                          -- 증가/감소/비슷/상승/하락/양호/부진/null
  bullets     jsonb not null default '[]'
);

create index if not exists section_text_file_id_idx on krei.section_text (file_id);
create index if not exists section_text_item_code_idx on krei.section_text (item_code);

-- 모든 표의 원형 (종류 상관없이 손실 없이)
create table if not exists krei.table_raw (
  id           bigserial primary key,
  file_id      int not null references krei.report on delete cascade,
  page         smallint not null,
  seq          smallint not null,             -- 문서 안 순서
  item_code    text references krei.item,
  item_label   text,
  title        text not null,
  unit         text,
  header       jsonb not null,                -- 머리글 줄(병합 머리글은 줄별 [{text,x0,x1}])
  rows         jsonb not null,                -- [[셀, …], …] 원문 문자열 그대로
  note         text,                          -- "주:" 이후
  source       text,                          -- "자료:" 이후
  table_type   text not null,                 -- production_outlook / shipment_outlook / monthly_price / growth_survey / other
  check_status text not null default 'not_checked'
               check (check_status in ('pass','fail','not_checked')),
  check_log    jsonb,                         -- 실패한 검사 항목
  unique (file_id, seq)
);

create index if not exists table_raw_file_id_idx on krei.table_raw (file_id);
create index if not exists table_raw_item_code_idx on krei.table_raw (item_code);

-- 정형 숫자 (long 포맷) — 검사 통과한 표에서만
create table if not exists krei.observation (
  id            bigserial primary key,
  file_id       int not null references krei.report on delete cascade,
  table_raw_id  bigint not null references krei.table_raw on delete cascade,
  item_code     text references krei.item,
  item_label    text,
  variety       text,                         -- 품종·작형 (후지, 신고, 노지, 방울 …), 없으면 null
  metric        text not null,                -- area / yield / production / shipment / wholesale_price
  basis         text not null,                -- current / prev / normal / yoy_pct / vs_normal_pct
  crop_year     smallint,                     -- 산(2026년산) 또는 해(2026년)
  period_label  text,                         -- 원문 "12월 이후", "8월 ~ 익년 6월", "10월"
  period_start  date,
  period_end    date,                         -- 모르면 null
  value         numeric not null,
  unit          text not null
);

create index if not exists observation_item_metric_start_idx on krei.observation (item_code, metric, period_start);
create index if not exists observation_file_id_idx on krei.observation (file_id);
create index if not exists observation_table_raw_id_idx on krei.observation (table_raw_id);

-- 생육·품질 분포
create table if not exists krei.survey_dist (
  id            bigserial primary key,
  file_id       int not null references krei.report on delete cascade,
  table_raw_id  bigint not null references krei.table_raw on delete cascade,
  item_code     text references krei.item,
  item_label    text,
  aspect        text not null,                -- 전반적인 생육상황, 착색 정도 …
  good          numeric,
  same          numeric,
  bad           numeric
);

create index if not exists survey_dist_file_id_idx on krei.survey_dist (file_id);
create index if not exists survey_dist_table_raw_id_idx on krei.survey_dist (table_raw_id);
create index if not exists survey_dist_item_code_idx on krei.survey_dist (item_code);

-- ---------------------------------------------------------------------------
-- 2. krei.report 에 파서 열 추가
-- ---------------------------------------------------------------------------
alter table krei.report
  add column if not exists parser_version text,
  add column if not exists parsed_at timestamptz;

-- ---------------------------------------------------------------------------
-- 3. 권한 및 RLS 설정
-- ---------------------------------------------------------------------------
grant usage on schema krei to anon, authenticated, service_role;
grant all on all tables in schema krei to service_role;
grant all on all sequences in schema krei to service_role;
grant select on all tables in schema krei to anon, authenticated;

alter table krei.section_text enable row level security;
revoke all on krei.section_text from anon, authenticated;
grant select on krei.section_text to anon, authenticated;
drop policy if exists "krei.section_text 누구나 읽기" on krei.section_text;
create policy "krei.section_text 누구나 읽기"
  on krei.section_text
  for select
  to anon, authenticated
  using (true);

alter table krei.table_raw enable row level security;
revoke all on krei.table_raw from anon, authenticated;
grant select on krei.table_raw to anon, authenticated;
drop policy if exists "krei.table_raw 누구나 읽기" on krei.table_raw;
create policy "krei.table_raw 누구나 읽기"
  on krei.table_raw
  for select
  to anon, authenticated
  using (true);

alter table krei.observation enable row level security;
revoke all on krei.observation from anon, authenticated;
grant select on krei.observation to anon, authenticated;
drop policy if exists "krei.observation 누구나 읽기" on krei.observation;
create policy "krei.observation 누구나 읽기"
  on krei.observation
  for select
  to anon, authenticated
  using (true);

alter table krei.survey_dist enable row level security;
revoke all on krei.survey_dist from anon, authenticated;
grant select on krei.survey_dist to anon, authenticated;
drop policy if exists "krei.survey_dist 누구나 읽기" on krei.survey_dist;
create policy "krei.survey_dist 누구나 읽기"
  on krei.survey_dist
  for select
  to anon, authenticated
  using (true);

-- ---------------------------------------------------------------------------
-- 4. 공개 뷰 (krei_api, security_invoker = true)
-- ---------------------------------------------------------------------------
grant usage on schema krei_api to anon, authenticated, service_role;

-- latest_outlook: 가장 최근 발행호의 전망치
create or replace view krei_api.latest_outlook
with (security_invoker = true) as
select distinct on (
  coalesce(o.item_code, ''),
  coalesce(o.item_label, ''),
  coalesce(o.variety, ''),
  o.metric,
  o.basis,
  coalesce(o.period_label, '')
)
  o.id,
  o.file_id,
  r.pub_date,
  r.pdf_link,
  o.item_code,
  o.item_label,
  o.variety,
  o.metric,
  o.basis,
  o.crop_year,
  o.period_label,
  o.period_start,
  o.period_end,
  o.value,
  o.unit
from krei.observation o
join krei.report r on r.file_id = o.file_id
order by
  coalesce(o.item_code, ''),
  coalesce(o.item_label, ''),
  coalesce(o.variety, ''),
  o.metric,
  o.basis,
  coalesce(o.period_label, ''),
  r.pub_date desc nulls last,
  r.file_id desc;

-- outlook_history: 같은 키의 발행호별 값 전체 (전망 수정 추적)
create or replace view krei_api.outlook_history
with (security_invoker = true) as
select
  o.id,
  o.file_id,
  r.pub_date,
  r.pdf_link,
  o.item_code,
  o.item_label,
  o.variety,
  o.metric,
  o.basis,
  o.crop_year,
  o.period_label,
  o.period_start,
  o.period_end,
  o.value,
  o.unit
from krei.observation o
join krei.report r on r.file_id = o.file_id
order by
  coalesce(o.item_code, ''),
  coalesce(o.item_label, ''),
  coalesce(o.variety, ''),
  o.metric,
  o.basis,
  coalesce(o.period_label, ''),
  r.pub_date desc nulls last,
  r.file_id desc;

-- headlines: 최신 발행호의 품목×섹션 헤드라인·방향
create or replace view krei_api.headlines
with (security_invoker = true) as
select distinct on (
  coalesce(s.item_code, ''),
  coalesce(s.item_label, ''),
  s.section
)
  s.id,
  s.file_id,
  r.pub_date,
  r.pdf_link,
  s.page,
  s.item_code,
  s.item_label,
  s.section,
  s.headline,
  s.direction,
  s.bullets
from krei.section_text s
join krei.report r on r.file_id = s.file_id
order by
  coalesce(s.item_code, ''),
  coalesce(s.item_label, ''),
  s.section,
  r.pub_date desc nulls last,
  r.file_id desc;

-- growth_survey: 최신 발행호의 생육 분포
create or replace view krei_api.growth_survey
with (security_invoker = true) as
select distinct on (
  coalesce(g.item_code, ''),
  coalesce(g.item_label, ''),
  g.aspect
)
  g.id,
  g.file_id,
  r.pub_date,
  r.pdf_link,
  g.item_code,
  g.item_label,
  g.aspect,
  g.good,
  g.same,
  g.bad
from krei.survey_dist g
join krei.report r on r.file_id = g.file_id
order by
  coalesce(g.item_code, ''),
  coalesce(g.item_label, ''),
  g.aspect,
  r.pub_date desc nulls last,
  r.file_id desc;

-- 뷰 권한 부여
revoke all on krei_api.latest_outlook from anon, authenticated;
grant select on krei_api.latest_outlook to anon, authenticated, service_role;

revoke all on krei_api.outlook_history from anon, authenticated;
grant select on krei_api.outlook_history to anon, authenticated, service_role;

revoke all on krei_api.headlines from anon, authenticated;
grant select on krei_api.headlines to anon, authenticated, service_role;

revoke all on krei_api.growth_survey from anon, authenticated;
grant select on krei_api.growth_survey to anon, authenticated, service_role;
