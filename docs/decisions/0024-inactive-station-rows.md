# 0024. 폐지·휴지 지점의 쌓인 줄 처리 규칙 (#65)

날짜: 2026-11-08 (사용자 지시. 문서만 있고 코드·DB 변경 없음)

## 배경
- 지점이 폐지·휴지(`stations.is_active = false`)가 되면 그 지점은 새 관측이 없다. 그런데 예전에 쌓인 줄을 어떻게 할지 정한 곳이 없었다.
- 실제로 있었던 일: 10/28 지점 사전이 105곳(운영 97 + 폐지·휴지 8)으로 늘었고, 10/29 PR #36(`57cc793`) 전의 적산온도 수집기가 사전 전체를 돌아 `heat_daily`에 폐지·휴지 8곳의 줄 8,760줄이 남았다. #55에서 백업 테이블(`heat_daily_bak_55`)을 만들고 SQL Editor에서 직접 지웠다(#63에서 11/9 재확인, 백업 테이블은 11/15 이후 삭제 예정).
- 같은 8곳(116 관악산, 164 무안, 175 진도(첨찰산), 176 대구(기), 187 성산, 214 삼척, 256 주암, 265 성산포)이 다른 표에도 줄을 남겼는지 읽기 전용 SQL로 확인했다.

### 확인 기록 (2026-11-08 17:42, 사용자 실행, #65에도 기록)
| 표 | 8곳 줄 수 |
|---|---:|
| `weather_daily` | 0 |
| `sun_daily` | 0 |
| `soil_water_daily` | 0 |
| `heat_daily` | 0 (#55에서 삭제) |
| `scenarios` | 0 → `water_balance_daily`는 `stn_id` 칸이 없어 `scenarios`를 거쳐 확인했고, 줄이 없음 |

**모두 0줄이라 지금 지울 것은 없다.** 이 문서는 앞으로를 위한 규칙이다.

## 결정
1. **새 줄을 만들지 않는다.** `is_active = false` 지점은 수집기가 새 줄을 쓰지 않는다. 적산온도·적산일사는 `active_stations`(결정 0017), 물 계산은 예보 지점 `has_forecast`만 돈다. (이미 그렇게 동작하며, 이 규칙은 그것을 유지한다.)
2. **쌓인 줄은 폐지가 확인된 날부터 90일 뒤에 정리한다.**
   - "폐지가 확인된 날"은 지점 사전 동기화(결정 0016)로 `is_active`가 true → false로 바뀐 것을 기록한 날(이슈·CHANGELOG에 적은 날짜)로 한다. 지금 8곳은 2026-10-28 기준이라 2027-01-26 이후가 대상이지만, 줄이 없으므로 해당 없음.
   - **90일 안에 `is_active = true`로 돌아오면(운영 복귀) 정리하지 않는다.** 90일은 #28 D4(전망 14일)·원문 90일 규칙과 맞춘 값이며, 바꾸려면 이 문서를 고친다.
3. **정리는 아래 수동 절차로 한다.** 줄이 많지 않아(대개 지점당 연 수백~천 줄) 새 도구를 만들지 않는다. 줄이 크게 쌓이는 일이 생기면 그때 retention 수집기(결정 0022)에 모드를 더할지 새로 정한다.
4. **지점 사전(`stations`, `scenarios` 등)은 이 절차로 지우지 않는다.** 사전의 주인은 CropMon이다.

## 수동 정리 절차 (SQL 틀)
쓰기가 있으므로 **사용자가 SQL Editor에서 승인 뒤에 실행**한다. 백업 테이블은 임시이며 마이그레이션이 아니다(#55와 같음). 아래 `<이슈>`는 그 정리 이슈 번호, 지점 목록은 `'116','164'`처럼 문자열로 쓴다.

0. **대상 확인(읽기만).** 폐지·휴지 지점과 줄 수를 본다.
   ```sql
   select stn_id, name_ko from stations where is_active = false order by stn_id;
   select count(*) from <표> where stn_id in (<지점 목록>);
   ```
1. **백업.** 지울 줄을 그대로 복사한다.
   ```sql
   create table <표>_bak_<이슈> as select * from <표> where stn_id in (<지점 목록>);
   ```
2. **줄 수 확인.** 백업 줄 수가 0단계 줄 수와 같아야 한다. 다르면 멈춘다.
   ```sql
   select (select count(*) from <표>_bak_<이슈>) as backup_rows,
          (select count(*) from <표> where stn_id in (<지점 목록>)) as live_rows;
   ```
3. **삭제.** 지우기 전후 전체 줄 수를 적어 둔다(운영 지점의 줄이 줄지 않았는지 확인).
   ```sql
   delete from <표> where stn_id in (<지점 목록>);
   ```
4. **지운 뒤 확인.** 대상 줄 0, 운영 지점 수와 지점별 줄 수가 예상대로인지 본다. #63의 확인 항목이 좋은 예다.
5. **며칠 뒤 백업 삭제.** 화면·수집에 문제가 없음을 보고 **7일 이상** 뒤에 지운다.
   ```sql
   drop table <표>_bak_<이슈>;
   ```
- **되돌리기(백업이 있는 동안):** `insert into <표> select * from <표>_bak_<이슈>;`

### `stn_id` 칸이 없는 표는 `scenarios`를 거친다
`water_balance_daily`처럼 지점 칸이 없고 `scenario_id`로만 이어지는 표는 `scenarios`(`scenario_id`, `stn_id`, `season_year`)에서 지점의 시나리오를 찾아 조건으로 쓴다.
```sql
select count(*) from water_balance_daily
 where scenario_id in (select scenario_id from scenarios where stn_id in (<지점 목록>));
-- 백업·삭제도 같은 조건: where scenario_id in (select scenario_id from scenarios where stn_id in (<지점 목록>))
```
- 전망 표(`water_outlook`, `water_outlook_summary`)는 지점이 `forecast_runs`(`run_id`로 연결)에 있다. 이 두 표는 이 절차가 아니라 결정 0022의 월 보관·삭제가 이미 맡는다.
- 줄이 있는 표를 빠뜨리지 않으려면 지점 칸이 있는 표(`weather_daily`, `sun_daily`, `soil_water_daily`, `heat_daily`)와 `scenarios`를 거치는 표(`water_balance_daily`)를 모두 센다.

## 할 일 (이번에는 문서에만 남긴다, 코드 변경은 하지 않음)
- 지점 사전 동기화(#27, 결정 0016) 때 **`is_active`가 true → false로 바뀐 지점을 보고서(`stations_diff.md`)에 적게** 한다. 그래야 "폐지가 확인된 날"이 기록으로 남고 90일을 셀 수 있다. 필요해지면 별도 이슈로 코드에 넣는다.

## 하지 않는 것
- 지금 삭제(대상 0줄), DB 구조·CropMon·cropwater 수정, 자동 삭제.
- `weather_daily` 95곳(비예보 지점) 줄의 정리: 폐지·휴지 지점이 아니라 #28 S2(물 계산 전 지점 확대)와 함께 결정한다(#65 1항, 줄 수·용량 보고만).
