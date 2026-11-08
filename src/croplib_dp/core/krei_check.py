"""KREI 농업관측 수집기 표 검사기 (순수 함수): 증감률 반올림 구간 재계산, 면적×단수≈생산량, 분포 합계.

결정 기록: docs/decisions/0023-krei-parse.md
"""


def interval_for_num(val_input: str | float | int) -> tuple[float, float, float]:
    """숫자 표기값으로부터 반올림 불확실성 구간 [min_val, max_val] 계산.

    예:
      '4.7'  (소수 1자리) -> (4.7, 4.65, 4.75)
      '-12.3' -> (-12.3, -12.35, -12.25)
      '34,901' (정수) -> (34901.0, 34900.5, 34901.5)
    """
    if isinstance(val_input, (int, float)):
        # float 인 경우 문자열 변환
        val_str = str(val_input)
    else:
        val_str = str(val_input)

    clean = (
        val_str.replace(",", "")
        .replace("%", "")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .strip()
    )
    val = float(clean)
    if "." in clean:
        decimals = len(clean.split(".")[1])
        half = 0.5 * (10 ** (-decimals))
    else:
        half = 0.5
    return val, val - half, val + half


def check_yoy_recalc(
    cur_input: str | float | int,
    prev_input: str | float | int,
    rep_input: str | float | int,
) -> tuple[bool, str]:
    """(당해 / 전년 - 1) * 100 증감률이 표기값과 일치하는지 반올림 구간으로 검사.

    고정 허용치 대신 당해와 전년의 유효자리 기준 구간에서 나올 수 있는 증감률의
    [min_ratio, max_ratio] 와 표기값의 [rep_min, rep_max] 가 겹치면 통과.
    """
    try:
        c_val, c_min, c_max = interval_for_num(cur_input)
        p_val, p_min, p_max = interval_for_num(prev_input)
        r_val, r_min, r_max = interval_for_num(rep_input)
    except (ValueError, TypeError) as e:
        return False, f"숫자 변환 실패: {e}"

    if p_min <= 0:
        return False, f"기준값이 0 이하입니다: {prev_input}"

    ratio_min = (c_min / p_max - 1.0) * 100.0
    ratio_max = (c_max / p_min - 1.0) * 100.0

    if max(ratio_min, r_min) <= min(ratio_max, r_max):
        return True, "pass"

    return (
        False,
        f"증감률 불일치: 표기 {rep_input} (구간 [{r_min:.2f}, {r_max:.2f}]), "
        f"계산 구간 [{ratio_min:.2f}, {ratio_max:.2f}]",
    )


def check_area_yield_prod(
    area: float,
    yield_: float,
    prod: float,
    unit_str: str | None = None,
) -> tuple[bool, str]:
    """생산량(천 톤) ≈ 면적(ha) × 단수(kg/10a) ÷ 100,000 검사 (상대오차 1% 이내).

    단위가 ha, kg/10a, 천 톤 형태가 아니면 검사를 건너뛰고 로그를 남긴다.
    """
    if unit_str and ("ha" not in unit_str or "kg/10a" not in unit_str or "천 톤" not in unit_str):
        return True, f"단위 다름으로 검사 건너뜀 (단위: {unit_str})"

    if area <= 0 or yield_ <= 0 or prod <= 0:
        return False, f"면적·단수·생산량이 0 이하입니다: area={area}, yield={yield_}, prod={prod}"

    expected = (area * yield_) / 100000.0
    rel_err = abs(prod - expected) / expected
    if rel_err <= 0.01:
        return True, "pass"

    return (
        False,
        f"생산량 불일치: 표기 {prod}, 계산 {expected:.2f} (상대오차 {rel_err * 100.0:.2f}% > 1%)",
    )


def check_dist_sum(good: float, same: float, bad: float) -> tuple[bool, str]:
    """생육조사 분포 합계 검사: 좋음 + 비슷 + 나쁨 = 100 ± 0.3."""
    total = good + same + bad
    if abs(total - 100.0) <= 0.3:
        return True, "pass"
    return False, f"분포 합계 불일치: {good} + {same} + {bad} = {total:.2f} (100 ± 0.3 벗어남)"
