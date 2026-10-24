# 구조

## 층
```
수집(collectors) → 계산(core) → 쓰기(io) → [DB: Supabase] → CropMon 화면
```
- `core/`는 바깥을 모르는 순수 함수라서 테스트가 쉽다.
- `io/`는 API 조회와 DB 쓰기를 맡고, 쓰기 전에 계약 검사를 한다.
- `collectors/`는 둘을 이어 주는 출처별 진입점이다.

## core 모듈
| 모듈 | 하는 일 | 원본 (cropwater) |
|---|---|---|
| `core/fao56.py` | FAO-56 식: ETo(식6), 풍속 환산, 일사, Kc 곡선·기후보정, 하루 물수지(식84~88), 결측일 | `fao56_core.py` |
| `core/daily_weather.py` | ASOS 하루 값 → 일 기상값·ETo, 빠진 날 채우기 (`weather_daily` 한 줄) | `cropwater_multi.compute_station` |
| `core/water_balance.py` | 관측 물수지와 상태 판정, 계약 상태 코드, 빠진 날은 아침 D+0 예보로 채움 (`water_balance_daily` 한 줄) | `fcst_wb.observed_wb`, `soil_status` |
| `core/kc.py` | Kc 기후보정: 생육 중기·후기 평균 풍속·최저습도 (10일 미만이면 기본값) | `cropwater_station` Kc 자동집계 |
| `core/forecast_daily.py` | 단기예보 항목 → 대상일별 하루 입력, 받은 발표 확인, 직전 발표로 대신하기 | `fcst_archive`, `kma_fcst.check_issue` |
| `core/solar.py` | 예보로 일사량 추정 (S1·S3·S4) | `rs_model` |
| `core/forecast_eto.py` | 예보 ETo·ETc (S4 고정 계수, 안 되면 S3) | `cropwater_fcst.forecast_table` |
| `core/outlook.py` | 3일 전망: 오차표, 세 경로(중심·빠르면·늦으면), 아침·저녁 출발, 관측 지연 (`water_outlook` 한 줄) | `fcst_wb.service_outlook` |

`io/coefficients.py`는 패키지 안 `data/`의 춘천 계수 파일(S3·S4 일사 계수, 예보 ETo 오차표)을 읽는다.

### 한 회차 계산 순서 (물1-3에서 수집기가 이 순서로 부른다)
1. ASOS 일자료 → `daily_weather` → 지점 일 기상값
2. 작물·재배 방식마다 `stage_climate` + `adjust_crop` → 기후보정 Kc
3. 단기예보(서비스 발표 + 앞선 24시간 발표) → `ForecastArchive` → `service_table` → `forecast_table`
4. `observed_water_balance(forecast_fill=fill_from_forecast(...), end=발표일 전날)` → 관측 물수지
5. `service_outlook` → `outlook_rows` → 3일 전망

계산 결과에는 `core.CALC_VERSION`(패키지 버전)을 붙인다. 수집기(물1-3)는 다음 PR에서 더한다.

## cropwater에서 가져올 때 달라지는 것
cropwater 운영 수집기(`cropwater_ops.py`)는 현장 PC의 파일(엑셀·CSV·폴더)에 상태를 둔다. 서버(GitHub Actions)는 매번 빈 컴퓨터에서 시작하므로 상태를 DB로 옮겨야 한다.

| cropwater (현장 PC) | croplib-dp (서버) |
|---|---|
| 01-Cycle 관측 엑셀(`obs`)에서 관측 ETo·Kc 읽기 | DB `weather_daily`에서 읽기 |
| `slot_log.csv` 슬롯 기록 | DB `forecast_runs` |
| `data/ops/` 원자료 보관 | Supabase Storage 압축 파일 |
| 서비스 엑셀 파일 생성 | 일 단위 결과를 DB에 쓰기 (엑셀은 11월 중 추가) |
| 작업 스케줄러 02:10·17:10 | GitHub Actions 02:20·17:20, 실패 시 1회 재시도 |

계산 코드(`fao56_core`, `rs_model`, `fcst_wb` 등)는 core로, API 조회(`kma_fcst`, `kma_grid`)는 io로 나눠 옮긴다. 현장 PC 수집기는 H3 시험이 끝날 때(11/20)까지 그대로 두고, 이후 두 결과를 비교해 서버로 일원화한다.

## 예약 실행 (이후 추가)
`.github/workflows/collect.yml` — 02:20·17:20(KST). 지연과 60일 자동 중지 대비는 CropMon 이행계획 3-2절을 따른다.
