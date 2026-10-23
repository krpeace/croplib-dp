# 0001. 파이프라인 저장소 분리와 서버 수집

날짜: 2026-10-23

## 결정
- 파이프라인은 CropMon(화면·DB)과 별도 저장소 `croplib-dp`로 둔다. 이름은 crop library data pipeline의 약어다.
- 두 저장소는 DB 데이터 계약으로만 연결한다. DB 구조의 주인은 CropMon이다.
- 수집·계산은 서버(GitHub Actions)에서 돌린다. cropwater의 현장 PC 수집기는 H3 시험(~11/20) 동안 그대로 둔다.
- 서버는 상태를 파일에 둘 수 없으므로 관측 자료·슬롯 기록은 DB, 원자료는 Storage에 둔다.
- 11/06은 물부터 시작하고, 온도·햇빛은 준비되는 대로 추가한다.

## 이유
- 화면 저장소와 수집 코드의 언어·배포·수명이 다르다(TypeScript/Vercel, Python/Actions).
- 현장 PC 수집기를 건드리면 H3 판정 자료가 오염된다.
