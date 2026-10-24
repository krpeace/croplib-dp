"""계산·판정 규칙. DB, API, 파일, 엑셀을 알지 못하는 순수 함수만 둔다. (FAO-56, 적산온도 등)"""

from croplib_dp import __version__

# 계산 결과(DB의 calc_version 열)에 남기는 버전. 계산식이나 판정 기준을 바꾸면 패키지 버전과 함께 올린다.
CALC_VERSION = __version__
