# 0039. 시설 재배 공개 조건은 비를 넣지 않고 "쓰는 물"(etc_only)만 낸다 (이슈 #128)

날짜: 2026-11-21

## 왜 고쳤나
CropMon이 포도 두 작형(비가림 · 시설가온)과 **2027년 포도 공개 계산 조건**(97개 지점 × 비가림 · 시설가온, 양토, 기본 재배 방식, 194줄)을 운영 DB에 넣었다(CropMon PR #185, 데이터 계약 v2.1). 물수지 수집기는 그해(`season_year`) 조건을 고르므로 2027-01-01부터 이 줄들을 계산한다. 시설가온은 시작이 01-01이라 첫날부터 돈다.
두 작형은 지붕 아래 시설 재배(`crop_seasons.is_protected = true`)다. 지금 코드는 이 열을 몰라서 그대로 두면 **바깥 비를 넣고 보통 판정(need · caution …)으로** 계산한다. 사용자 결정(2026-11-17 · 11-18): 시설 재배는 비를 계산에 넣지 않고, 공개 조건은 판정하지 않고 "쓰는 물"(작물이 쓰는 물 = ETc)만 낸다. 가정 관수(관수 시작점을 넘으면 물을 준 것으로 보는 톱니 곡선)는 쓰지 않는다. 화면은 "1,000 m²당 하루 약 ○톤"(= ETc mm)으로 보인다.

## 결정
1. **시설 여부는 인자로 받는다(core는 DB를 모른다).** `observed_water_balance(…, protected=False)`와 `service_outlook_etc_only(…)`. 수집기가 조건마다 사전 `crop_seasons[season_type_id].is_protected`를 읽어 넘긴다.
2. **시설 조건의 관측 물수지**: 비를 넣지 않고 Dr도 계산하지 않는다. 작기 안(Kc > 0)은 `status = 'etc_only'`, 작기 밖은 `dormant`(판정 순서 `dormant → etc_only → 그 밖`). ETo · Kc · ETc는 채우고 `irr_net_mm = 0`, **Dr 관련 열(`dr_prev_mm` · `ks` · `etc_adj_mm` · `dp_mm` · `dr_mm`)은 작기 안팎 모두 비운다**(Dr를 계산하지 않으므로, 검토 채팅 승인 2026-11-21). `eto_src`는 노지와 같은 규칙(관측 / 예보 / 없음)이고 `filled`는 그날 값이 채움이면 참이다(Dr 사슬이 없다). `rain_mm`에는 바깥 비 값을 기록으로 남긴다(화면이 시설 포도에서 지운다).
3. **시설 조건의 3일 전망**: `water_outlook`은 ETo · ETc 예보와 `etc_err_mm`만 채우고 `dr_*`는 비우고 `status = 'etc_only'`(그날 Kc ≤ 0이면 `dormant`). `water_outlook_summary`는 `status_start = 'etc_only'`(회차 발표 날 Kc ≤ 0이면 `dormant`), `dr_start_mm` · `need_*_in` · `advice_*`는 비우고 **`etc_sum_mm`(3일 ETc 합)과 `etc_sum_err_mm`은 채운다**(화면의 "3일 동안 약 ○톤"). `start_date`는 노지와 같이 아침 회차는 어제, 저녁 회차는 오늘이다. 비 열(`rain_fcst_mm` · `rain_expected_mm` · 요약의 비 합계)은 예보 값을 기록으로 남긴다.
4. **노지(사과 · 배추)는 한 자리도 바뀌지 않는다.** 시설 갈래는 따로 두었고, 전망 앞부분(예보 날 표 · 3일 합)은 `_forecast_days` · `_cum3`로 빼서 두 갈래가 같이 쓰되 코드를 그대로 옮겼다. 시험이 이를 지킨다: 시설 갈래를 넣기 전 `main`(`2db9fb0`)의 코드로 같은 입력(가짜 API · 가짜 시계, 아침 · 지연 · 저녁 회차, 사과 k3 · k1 · 배추)의 결과를 뽑아 `tests/data/open_field_golden.txt`에 저장하고(만드는 법은 `tests/data/make_open_field_golden.py` 머리말), 지금 코드의 결과를 상대 1e-9로 비교한다. 같은 지점에 시설 조건을 섞어도 노지 줄과 `forecast_runs`가 같다.
5. **시설 조건은 회차 기록을 바꾸지 않는다.** Dr 이어 계산용 준비(관측 물수지 표 · 지연일 채움)를 하지 않으므로 `forecast_runs.obs_prev_date` · 백업 표시는 노지 조건의 값 그대로다. 시설 조건만 있는 지점은 `obs_prev_date`가 비어 있고(비어도 되는 열) 회차는 정상이다.
6. **`is_protected`를 읽지 못하면 조용히 거짓으로 두지 않는다.** 사전 `crop_seasons` 줄에 열이 아예 없거나 값이 `null`이면 그 조건은 계산하지 않고 `problems`에 남긴다(회차 실패로 보임). 거짓(노지)으로 대신하면 바로 이 이슈가 막으려던 "바깥 비를 넣고 보통 판정"이 조용히 일어난다. DB 기본값은 거짓이라 정상이면 생기지 않는다. 시험용 가짜 사전(`tests/test_collector.py`)의 작형 줄에는 `is_protected=False`를 달았다.
7. **계약 검사**: `water_balance_daily.status`와 `water_outlook_summary.status_start`에 `etc_only`, `water_outlook.status`는 `wet` 없이 지금 목록 + `etc_only`. Dr 열이 비어 있어도 통과한다(`DR_COLUMNS` 검사는 빈 값을 건너뜀). `STATUSES`(core)에도 `etc_only`.
8. **알림**: `brief_text`는 작물 이름(`CROP_KO`) 대신 **작형 이름**(`crop_seasons.name_ko`)을 쓴다: 같은 지점에 포도 줄이 둘 붙기 때문이다("포도 비가림" · "포도 시설가온"). 이름이 없으면 작물 이름(`CROP_KO`, `"grape": "포도"` 추가). `etc_only`는 "포도 비가림 쓰는 물 3.2mm"(마지막 날 ETc), 작기 밖은 "dormant". 이 규칙 때문에 **배추 줄은 "배추" → "고랭지배추"로 바뀐다**(데이터 계약의 작형 이름과 같음, 승인). 10곳 × 기본 조건 4개(사과 · 포도 둘 · 배추)는 2,111바이트로 알림 칸(약 4KB, 결정 0030)에 들어간다(시험 < 3,800). 실행 요약(`summary_text`)도 "Dr … mm" 대신 시설 조건은 "쓰는 물 … mm"로 적는다.
9. **`CALC_VERSION`은 올리지 않는다.** 노지 값이 바뀌지 않고(시험), 포도 시설 줄은 새로 생기는 줄이다. 올리면 모든 줄의 `calc_version`이 바뀌어 쓰기만 늘어난다.
10. 소급(`run_wb_backfill`)도 같은 갈래(`protected`)를 쓴다.

## 하지 않은 것
- 농장 계산(`farm_water_*`, `farm_calc.yml`, 관수 기록이 생기면 첫 기록 날부터 판정)은 다음 이슈다. 이번에는 공개 조건(`scenarios`)만.
- 시설 안 ETo 보정(바깥 ETo를 그대로 쓴다): 고도화.
- DB 구조(표 · 열 · check)는 CropMon 몫이라 고치지 않았다(`is_protected` · `etc_only`는 CropMon v2.1이 이미 넣었다).
- 감시(`monitor`)는 `water_balance_daily` · `water_outlook`의 상태를 읽지 않아 `etc_only` · 빈 Dr를 오류로 볼 곳이 없다(읽는 표: `forecast_runs` · `stations` · `heat_daily` · `soil_water_daily` · `krei.report`).

## 확인
- 병합 전 미리 돌려 보기(쓰기 없음): 실제 2027 공개 조건(운영 DB 읽기 전용 조회 값) + 가짜 ASOS · 가짜 예보로 2027-01-05 · 2027-05-10 아침 회차를 돌려 시설가온 · 비가림 한 지점씩 며칠 치 줄과 계약 검사(통과)를 PR에 남겼다.
- 병합 뒤(2027-01-02 회차): 포도 시설가온 줄 수 · 상태 `etc_only`만 · Dr 빔 · 노지 줄 수 변화 없음을 읽기 전용 조회로 본다(검토 채팅이 기록).
