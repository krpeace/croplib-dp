# croplib-dp

CropMon의 데이터 파이프라인 (crop library data pipeline). 공공 API에서 자료를 받아 계산하고 결과를 DB에 씁니다.

| 저장소 | 역할 |
|---|---|
| [CropMon](https://github.com/krpeace/CropMon) | 화면, DB 구조, 데이터 계약 |
| **croplib-dp (이 저장소)** | 출처별 수집기, 계산, 예약 실행 |
| [cropwater](https://github.com/krpeace/cropwater) · [cropweather](https://github.com/krpeace/cropweather) | 연구 원본 (읽기 전용 참고) |

두 저장소는 DB 테이블 약속(데이터 계약)으로만 연결됩니다. 이 저장소는 DB에 쓰기만 하고, 구조는 CropMon이 정합니다.

## 지금 상태 (v0.1.0 준비 중)
관측 계산(ETo, 관측 물수지, 토양수분 상태)과 예보 계산(3일 전망)을 옮겼습니다. 11/06 공개를 목표로 수집기와 DB 업로드를 이어서 만듭니다. 일정은 CropMon 기획 문서를 따릅니다.

## 한 회차 돌려 보기
```
DATA_GO_KR=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python -m croplib_dp.collectors.water --slot "2026-10-25 02" --no-wait
```
결과는 `out/` 폴더에 파일로 남습니다(DB 쓰기는 다음 단계). GitHub Actions의 `collect-dryrun`을 눌러도 같은 일을 합니다.

## 개발
```
pip install -e ".[dev]"
ruff check . && ruff format --check . && pytest -q
```
키는 `.env.example`을 `.env`로 복사해 채웁니다. 서버에서는 GitHub Secrets를 씁니다. 작업 규칙은 [CLAUDE.md](CLAUDE.md), 구조는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)를 봅니다.

## 라이선스
MIT, FarmOS Corp. (㈜파모스)
