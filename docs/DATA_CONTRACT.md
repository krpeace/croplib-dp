# 데이터 계약 (참조)

DB 테이블·컬럼·단위·발표 회차 정의는 CropMon 저장소의 `docs/DATA_CONTRACT.md`가 원본이다. 이 저장소는 그 계약대로 쓰기만 한다.

- 계약 버전을 올릴 때는 CropMon에서 마이그레이션과 계약서를 함께 고치고, 이 저장소의 업로드 코드를 새 계약에 맞춘다. 두 PR에 서로의 링크를 단다.
- 업로드 전 검사(컬럼, 단위, 값 범위)는 `src/croplib_dp/io/`에 둔다.

현재 맞춘 계약 버전: v1.5 (2026-10-28, 작물 공통 토양 물 상태 `soil_water_daily`, `soil_types.is_estimated` 및 추정 3종, `crops.common`, CropMon#35·결정 0014). v1.4: 적산온도 `heat_daily`·적산일사 `sun_daily`, 작물 사전 `t_base_c` (2026-10-26). v1.3: 3일 전망 요약 `water_outlook_summary` (2026-10-28, 구조 그대로·문구만: `status_start` 휴면기 판정 기준 = 회차 발표 날짜, CropMon#37·결정 0015). v1.2: 3일 전망 선행일 0~4, 원자료 버킷 `raw`. 업로드 전 검사는 `io/contract.py`. 계산 결과의 열 이름과 상태 코드를 맞췄다. 재배 방식별 Kc(`crop_kc_sets`)는 기후보정 전 표값이고, 보정은 `core/kc.py`가 한다.

## `soil_water_daily` (계약 v1.5, CropMon#35·결정 0014)
- 키: `(stn_id, soil_code, date)`. 대표 10곳(`has_forecast`) × 토성 12종(FAO-56 Table 19 9종 + Saxton & Rawls 추정 3종).
- 고정 조건: 겉흙 깊이 Zr = 0.5 m, Kc = 1.0, p = 0.40, 해마다 3/1에 Dr = 0(포장용수량)으로 시작해 11/30까지 계산. 12~2월은 `status = 'winter'`, `dr_mm`·`aw_pct`·`dp_mm`·`days_to_dry`는 빈 칸.
- 상태 코드 6종(위에서부터 처음 맞는 것): `winter`(12~2월), `pool`(끝 Dr < 0.05 mm 이고 당일 또는 전날 DP ≥ 1 mm), `ample`(`aw_pct` ≥ 80), `fair`(60 ≤ `aw_pct` < 80), `dry`(30 ≤ `aw_pct` < 60), `very_dry`(`aw_pct` < 30).
- `days_to_dry`: `fair`일 때만 ⌈(RAW − Dr) ÷ 7일 평균 ETo⌉ (평균 ETo 0이면 빈 칸).
- 작물 공통 적산온도는 `heat_daily`에 `crop_id = 'common'`(기준온도 5℃, 단계 1,000·2,000 ℃·일)으로 저장한다.

## `water_outlook_summary.status_start` (계약 v1.3 문구, CropMon#37)
- `status_start`: 오늘 아침 흙 상태 (다섯 가지 코드). 부족한 물은 출발 날짜 끝 값으로 판정하고, **휴면기는 회차 발표 날짜 기준**. 과습은 출발 날짜가 작기 안이고 관측 물수지 날일 때만.
- 3일 전망 요약(`status_start`)의 휴면기는 회차 발표 날짜(한국 날짜)의 Kc로 판정합니다. 작기 마지막 다음 날 아침에 관수를 권하거나, 작기 첫날 아침에 휴면기로 나오는 하루 어긋남을 막기 위해서입니다 (croplib-dp 결정 0015).
