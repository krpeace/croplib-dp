# 데이터 계약 (참조)

DB 테이블·컬럼·단위·발표 회차 정의는 CropMon 저장소의 `docs/DATA_CONTRACT.md`가 원본이다. 이 저장소는 그 계약대로 쓰기만 한다.

- 계약 버전을 올릴 때는 CropMon에서 마이그레이션과 계약서를 함께 고치고, 이 저장소의 업로드 코드를 새 계약에 맞춘다. 두 PR에 서로의 링크를 단다.
- 업로드 전 검사(컬럼, 단위, 값 범위)는 `src/croplib_dp/io/`에 둔다.
- **하루 강수량(`rain_mm`, 예보 `rain_fcst_mm`·`rain_expected_mm`)의 상한은 1,000 mm**다(`io/contract.py`의 `DAILY_RAIN_MAX_MM`, 결정 0027). 옛 500 mm는 2026-08-17 294 거제의 실제 654.3 mm를 막았다. DB에는 이 범위 제약이 없다(CropMon 마이그레이션 확인). CropMon 계약서 306행의 "0~500" 문구 정정은 결정 0027의 요청 문안.

현재 맞춘 계약 버전: v1.5 (2026-10-28, 작물 공통 토양 물 상태 `soil_water_daily`, `soil_types.is_estimated` 및 추정 3종, `crops.common`, CropMon#35·결정 0014). v1.4: 적산온도 `heat_daily`·적산일사 `sun_daily`, 작물 사전 `t_base_c` (2026-10-26). v1.3: 3일 전망 요약 `water_outlook_summary` (2026-10-28, 구조 그대로·문구만: `status_start` 휴면기 판정 기준 = 회차 발표 날짜, CropMon#37·결정 0015). v1.2: 3일 전망 선행일 0~4, 원자료 버킷 `raw`. 업로드 전 검사는 `io/contract.py`. 계산 결과의 열 이름과 상태 코드를 맞췄다. 재배 방식별 Kc(`crop_kc_sets`)는 기후보정 전 표값이고, 보정은 `core/kc.py`가 한다.

## 농장별 계산 결과 `farm_water_daily`·`farm_water_outlook`·`farm_water_outlook_summary` (계약 v2.1, 결정 0040)
- 공개 표와 같은 열에서 `scenario_id` → `farm_id`. `farm_water_daily` 키 `(farm_id, date)`(+ `farm_season_id` · `irr_l` · `irr_net_mm` · `taw_mm` · `raw_mm`), `farm_water_outlook` 키 `(farm_id, target_date)` · **최신 회차만**(`run_id` → `forecast_runs`), `farm_water_outlook_summary` 키 `farm_id` · 최신만. 상태 코드는 공개 표와 같은 여섯(`etc_only` 포함, 전망은 `wet` 없음). 이 저장소가 서버 키로 쓴다(`farm_calc.yml`).
- 계산 규칙: 작기는 `farm_seasons`의 날짜로 고른다(해를 넘어도 Dr 이어받음). 관수 mm = 그날 합(L) ÷ 관수 면적 × 관수효율 0.95, 작기 밖 기록은 넣지 않는다(`irr_l`도 0). 시설은 그날까지 기록 0건인 날 `etc_only`, **첫 기록 날의 끝 Dr = 0**, 다음 날부터 비 0 물수지. 노지는 공개 조건과 같은 값. 작물 공통 농장은 계산하지 않는다. 오늘 기록은 3일 전망의 오늘(lead 0, 저녁 회차는 출발일) 단계에 들어간다.
- 입력: 관측은 `weather_daily`, 3일 전망은 같은 회차의 공개 전망 줄(`water_outlook`)로 되살린 예보표(공공 API 호출 없음). 시작 전인 작기만 있는 농장은 물수지 · 전망 · 요약 줄이 없다.
- `farm_calc_status`는 `done_at` · `result`(`ok`/`failed`) · `error`(짧은 말) · `calc_version`만 쓴다(`requested_at` · `recalc_from`은 DB 트리거). `done_at`은 그 계산이 대기 줄을 읽기 시작한 시각(대기 중이던 농장은 읽은 `requested_at` 값).

## 시설 재배 공개 조건 `etc_only` (계약 v2.1, CropMon PR #185·결정 0039)
- `crop_seasons.is_protected = true`(포도 비가림 · 시설가온)는 지붕 아래라 **비를 계산에 넣지 않고**, 공개 조건은 **판정하지 않고 "쓰는 물"(ETc)만** 낸다. 사과 · 고랭지배추는 거짓이라 바뀌지 않는다.
- 상태 값 `etc_only`: `water_balance_daily.status` · `water_outlook.status` · `water_outlook_summary.status_start`. 판정 순서 `dormant`(작기 밖, Kc ≤ 0) → `etc_only` → 그 밖. `water_outlook`은 지금처럼 `wet` 없음.
- 물수지: ETo · Kc · ETc는 채우고 `irr_net_mm = 0`, **Dr 관련 열(`dr_prev_mm` · `ks` · `etc_adj_mm` · `dp_mm` · `dr_mm`)은 모두 비움**(작기 밖 `dormant` 줄도). `rain_mm`에는 바깥 비 값을 기록으로 남기고 화면이 시설 포도에서 지운다.
- 전망: `water_outlook`은 ETo · ETc · `etc_err_mm`만 채우고 `dr_*`는 비움. `water_outlook_summary`는 `dr_start_mm` · `need_*_in` · `advice_*`를 비우고 **`etc_sum_mm`(3일 ETc 합) · `etc_sum_err_mm`은 채움**("3일 동안 약 ○톤").
- 사전 `crop_seasons`에서 `is_protected`를 읽지 못하면(열이 없거나 `null`) 그 조건은 계산하지 않고 수집 결과 문제 목록에 남긴다(거짓으로 대신하지 않음).

## 적산 계열 `heat_series_daily`·`sun_series_daily` (계약 v2.2, CropMon PR #188·결정 0038)
- 적산온도는 작목 기준온도와 기상청 기온만으로 계산하므로, 값을 가르는 것은 **기준온도**와 **누적 시작 월-일**(`cum_start_mmdd`, `'MM-DD'`) 둘뿐이다(= 적산 계열). `crop_id`는 없다.
- `heat_series_daily` 키 `(stn_id, t_base_c, cum_start_mmdd, date)`, 값 열은 `heat_daily`와 같다(`gdd_day`·`gdd_cum`·`gdd_normal_day`·`gdd_normal_cum`·`lead_days`·`normal_from`·`normal_to`·`filled`·`calc_version`). `t_base_c`는 자릿수 없는 `numeric`(-10~30).
- `sun_series_daily` 키 `(stn_id, cum_start_mmdd, date)`, 값 열은 `sun_daily`와 같다.
- 계열 목록은 뷰 `crop_accum_series`(작형마다 한 줄 + 작물 공통 한 줄: `crop_id`, `season_type_id`, `heat_t_base_c`, `heat_cum_start_mmdd`, `sun_cum_start_mmdd`)에서 읽는다. 시설가온은 `heat_*`가 비어 적산온도를 내지 않는다.
- **누적 시작 전 날은 줄을 쓰지 않는다.** 누적·평년 누적·`lead_days`는 누적 시작부터 센다. 일값은 시작이 달라도 같다. 식·평년·빈 날 처리는 옛 표와 같다. (5, 01-01) 계열 = `heat_daily`의 `crop_id = common` 줄, `sun_series_daily`의 01-01 계열 = `sun_daily`.
- 쓰기 순서는 옛 표 → 새 표. 새 표가 실패해도 옛 표는 그대로 두고 수집 결과(문제 목록)에 남긴다. 옛 표는 3단계(CropMon 화면 전환) 전까지 지금 그대로 쓴다. 업로드 전 검사는 `io/contract.py`의 `SPECS`.

## `soil_water_daily` (계약 v1.5, CropMon#35·결정 0014)
- 키: `(stn_id, soil_code, date)`. 대표 10곳(`has_forecast`) × 토성 12종(FAO-56 Table 19 9종 + Saxton & Rawls 추정 3종). `OBS_ALL_STATIONS=true`면 운영 지점(`is_active`) 전부(관측만 하는 지점은 이 표와 `weather_daily`만, 결정 0026). CropMon 계약서의 "대표 10곳" 문구 정정은 CropMon 요청 P5.
- 고정 조건: 겉흙 깊이 Zr = 0.5 m, Kc = 1.0, p = 0.40, 해마다 3/1에 Dr = 0(포장용수량)으로 시작해 11/30까지 계산. 12~2월은 `status = 'winter'`, `dr_mm`·`aw_pct`·`dp_mm`·`days_to_dry`는 빈 칸.
- 상태 코드 6종(위에서부터 처음 맞는 것): `winter`(12~2월), `pool`(끝 Dr < 0.05 mm 이고 당일 또는 전날 DP ≥ 1 mm), `ample`(`aw_pct` ≥ 80), `fair`(60 ≤ `aw_pct` < 80), `dry`(30 ≤ `aw_pct` < 60), `very_dry`(`aw_pct` < 30).
- `days_to_dry`: `fair`일 때만 ⌈(RAW − Dr) ÷ 7일 평균 ETo⌉ (평균 ETo 0이면 빈 칸).
- 작물 공통 적산온도는 `heat_daily`에 `crop_id = 'common'`(기준온도 5℃, 단계 1,000·2,000 ℃·일)으로 저장한다.

## `weather_daily.rs_source` (C7, 결정 0020·0026 — CropMon 칸 반영 대기)
- 칸: 문자, 비어도 됨. 허용값 `observed`(관측 일사) / `sunshine`(일조시간, 식35) / `temp_range`(기온차, 식50) / 비움(ETo 없음). ETo의 일사를 어디서 얻었는지(일사 없는 32곳의 신뢰도 표시).
- 이 저장소는 올리기 직전에 DB에 칸이 있는지 확인해(`column_exists`) 있으면 올리고, 없거나 확인하지 못하면 그 칸만 빼고 올린다(요약에 경고). 계약 검사(`io/contract.py`)에는 허용값이 있다.

## `water_outlook_summary.status_start` (계약 v1.3 문구, CropMon#37)
- `status_start`: 오늘 아침 흙 상태 (다섯 가지 코드). 부족한 물은 출발 날짜 끝 값으로 판정하고, **휴면기는 회차 발표 날짜 기준**. 과습은 출발 날짜가 작기 안이고 관측 물수지 날일 때만.
- 3일 전망 요약(`status_start`)의 휴면기는 회차 발표 날짜(한국 날짜)의 Kc로 판정합니다. 작기 마지막 다음 날 아침에 관수를 권하거나, 작기 첫날 아침에 휴면기로 나오는 하루 어긋남을 막기 위해서입니다 (croplib-dp 결정 0015).
