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

## 수집기 (물1-3)
| 모듈 | 하는 일 |
|---|---|
| `io/kma.py` | 단기예보·ASOS 조회: 키 인코딩, 여러 쪽 이어 받기, 응답 해석(JSON·XML 오류), 결과 구분(ok·nodata·retry·fatal), 기록에서 키 감추기 |
| `io/dictionaries.py` | Supabase에서 사전 자료 6개 표 읽기, 계산 입력(StationMeta·CropParams·Soil)으로 바꾸기 |
| `collectors/water.py` | 한 회차: 서비스 발표 재시도(발표 + 10분부터 0~80분, 마감 + 90분) → 앞선 24시간 발표 → 지점마다 ASOS(작기 시작 ~ 어제) → 계산 → DB 표 모양의 줄 |

- 회차 결과(`forecast_runs.result`): 정시 성공 / 백업: S3 / 백업: 직전 발표 / 지연 수집 / 실패. 예보 지점(`has_forecast`)만 회차 기록을 남긴다. 예보가 없는 지점은 관측 물수지만 계산하고, ASOS 결과는 수집 기록(`collect_log.jsonl`)과 요약에 남긴다.
- 서버는 받은 예보를 쌓아 두지 않는다. 그래서 하루 넘게 지난 관측 결측일은 예보로 채우지 못하고 앞 7일 평균이 된다. 물3에서 예보 원자료를 Storage에 쌓으면 현장 PC처럼 채운다.
- 실행: `python -m croplib_dp.collectors.water [--slot "2026-10-25 02"] [--stations 101] [--no-wait] [--out out]`. 환경변수 `DATA_GO_KR`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`. 결과는 `out/<발표시각>/`에 표 4개(JSON), 수집 기록, 요약, 응답 원문(raw, 압축). DB 쓰기는 물3.
- ASOS 조회가 일시 오류(시간 초과·서버 오류)면 30초·90초·3분 뒤 세 번 더 시도한다. 시험 실행에서 공공데이터포털이 몇 분 동안 모든 요청에 응답하지 않은 적이 두 번 있었다.
- 시험 실행: GitHub Actions → `collect-dryrun` → Run workflow. 손으로 누를 때만 돌고, 결과 파일은 그 실행의 Artifacts(7일 보관)에 올라간다. 요약과 오류는 실행의 알림(annotation)에도 남겨, API로 결과를 확인할 수 있다.

## DB에 올리기 (물3)
| 모듈 | 하는 일 |
|---|---|
| `io/contract.py` | 올리기 전 계약 검사(v1.2): 열 이름, 빈 칸, 값 범위(ETo 0~15, 기온 −40~50, 습도 0~100, Ks 0~1, Dr 0~TAW, 선행일 0~4 등), 정해진 이름(상태 코드, 회차 결과), 키 중복 |
| `io/supabase_write.py` | PostgREST 덮어쓰기(같은 키는 새 값으로, 500줄씩), Storage 올리기 |
| `collectors/water.publish` | 계약 검사 → 원자료(Storage `raw`) → `weather_daily` → `water_balance_daily` → `forecast_runs`(번호 받기) → `water_outlook` |

- 계약 검사에 걸리면 계산 결과는 올리지 않고, 회차 기록만 "실패"와 이유(`error`)를 담아 올린다. 종료 코드 1.
- 원자료는 예보 발표마다 한 파일(`fcst/<격자>/<연>/<월>/<발표>.json.gz`)이고, 다시 받으면 덮어쓴다. ASOS 응답은 언제든 다시 받을 수 있어 보관하지 않는다. 원자료 올리기가 실패하면 문제로 남기고 `raw_path`를 비운 채 계속한다.
- 같은 회차를 다시 돌리면 줄이 늘지 않고 새 값으로 바뀐다. 회차 기록(`forecast_runs`)도 마지막 실행으로 바뀐다.
- 실행: 수집기에 `--write`. 시험 실행 버튼에서는 `write`를 켠다.

## 예약 실행 (물2)
| 파일 | 언제 | 하는 일 |
|---|---|---|
| `.github/workflows/collect.yml` | 매일 02:15·02:35·02:55, 17:15·17:35·17:55 (KST), 손으로도 | 회차 정하기 → 수집·계산·DB 쓰기(마감까지 재시도) → 실패하면 5분 뒤 한 번 더 → 요약·알림 |
| `.github/workflows/monitor.yml` | 매시 40분, 수집 끝날 때 | 수집 감시: 문제가 있으면 "수집 경고" 이슈를 열거나 댓글, 사라지면 닫음 |
| `.github/workflows/keepalive.yml` | 매달 1일 12:40 (KST) | 60일 무활동 자동 중지를 막으려고 예약 실행(collect, monitor, keepalive)을 API로 다시 켬 |
| `.github/workflows/collect-dryrun.yml` | 손으로만 | 시험 실행 (DB 쓰기는 선택) |

- 세 실행은 같은 `concurrency: collect` 묶음이라 동시에 돌지 않는다(앞 실행이 끝날 때까지 기다림).
- 회차는 시작 시각으로 한 번 정하고(`current_slot`), 다시 실행할 때도 같은 회차를 쓴다.
- GitHub 예약 실행은 몰리면 늦거나 건너뛴다(10/25 첫 실행 2시간 9분 지연). 그래서 회차마다 세 번 걸고, 이미 "정시 성공"인 회차는 바로 끝낸다(`--skip-if-done`). 지연·백업·실패면 다시 돌아 결과를 낫게 한다.
- 두 번 모두 실패하면 실행이 실패로 끝나고 GitHub가 저장소 주인에게 실패 메일을 보낸다. 회차 기록(`forecast_runs`)에도 "실패"와 이유가 남는다. 

## 수집 감시 (공3)
| 모듈 | 하는 일 |
|---|---|
| `core/monitor.py` | 경고 규칙: 마지막 회차가 13시간 넘게 지남(오류), 마지막 회차 실패(오류), 지연 수집·백업(경고), 18시 뒤에도 어제 ASOS 없음(경고) |
| `io/github_issue.py` | "수집 경고" 이슈 열기·댓글·닫기. 새 경고가 생길 때만 댓글(같은 경고로 메일이 쌓이지 않게) |
| `collectors/monitor.py` | DB에서 최근 서버 회차 기록·지점별 관측 마지막 날을 읽어 규칙을 돌리고 이슈에 반영 |

알림은 GitHub 이슈로 한다. 저장소 주인에게 이슈·댓글이 메일로 가므로 메일 서버 키가 필요 없다. 다른 주소로 받으려면 메일 발송 서비스를 따로 붙인다(지금은 안 함).
