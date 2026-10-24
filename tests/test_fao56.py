"""FAO-56 핵심 계산 테스트.

cropwater `tests/test_fao56.py`(feat/cycle02-forecast)에서 계산 부분을 그대로 가져왔다.
같은 입력에서 같은 숫자가 나오는지 확인한다. 엑셀·CSV 관련 테스트는 이 저장소에 해당 코드가 없어 뺐다.

검증 기준
  - FAO-56 식(11) svp, 식(21) Ra, 식(47) wind_2m
  - FAO-56 식(6) ETo Penman-Monteith
  - FAO-56 식(82)(83)(84)(85)(88) TAW·RAW·Ks·DP 물수지 (식88은 원식: 당일 ETc를 뺌)
  - FAO-56 그림(25) Kc 생육단계 보간
  - 실증값: 춘천(ASOS 101) 2026-04-01 실측 기반
"""

import datetime as dt

import pytest

from croplib_dp.core.fao56 import (
    eto_penman_monteith,
    extra_radiation,
    kc_climate_adjust,
    kc_of_date,
    missing_dates,
    prev_mean,
    raw_mm,
    svp,
    taw_mm,
    wb_step,
    wind_2m,
)
from croplib_dp.core.water_balance import Soil, observed_water_balance

ABS_TOL_ETO = 0.05  # mm/day
ABS_TOL_KS = 0.001


# 1. 포화증기압 svp(T) — FAO-56 식(11)
class TestSvp:
    def test_0c(self):
        """0°C → 포화증기압 = 0.6108 kPa"""
        assert abs(svp(0) - 0.6108) < 0.001

    def test_20c(self):
        """20°C → 포화증기압 ≈ 2.338 kPa"""
        assert abs(svp(20) - 2.338) < 0.005

    def test_monotone(self):
        """기온 상승 시 포화증기압 단조증가"""
        vals = [svp(t) for t in [0, 10, 20, 30, 40]]
        assert all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))


# 2. 천문일사 Ra — FAO-56 식(21)
class TestExtraRadiation:
    def test_equator_march(self):
        """적도(위도 0), 3월 15일(J=74): Ra 35~38 MJ/m²/day"""
        assert 35.0 < extra_radiation(0, 74) < 38.0

    def test_seasonal_variation(self):
        """춘천(37.9°N): 7월 Ra > 1월 Ra"""
        assert extra_radiation(37.9, 196) > extra_radiation(37.9, 15)


# 3. 풍속 환산 — FAO-56 식(47)
class TestWind2m:
    def test_10m_reduction(self):
        """10 m → 2 m: u2 = u10 × 4.87/ln(67.8×10−5.42) ≈ u10 × 0.748"""
        assert abs(wind_2m(2.0, zw=10) / 2.0 - 0.748) < 0.01

    def test_2m_pass_through(self):
        """측정 높이 2 m이면 값 그대로"""
        assert abs(wind_2m(3.0, zw=2) - 3.0) < 0.01


# 4. ETo — FAO-56 식(6)
class TestEtoPM:
    """춘천(ASOS 101) 2026-04-01 실측값 기반. cropwater 검증값: ETo ≈ 1.77 mm/day"""

    def _base_params(self, **overrides):
        p = dict(
            Tmax=17.7,
            Tmin=5.2,
            Rs=10.47,
            u2=wind_2m(1.0, zw=10),
            ea=1.03,
            elev=75.82,
            lat=37.90,
            J=91,
            Pa=1003.5,  # hPa (함수 안에서 ×0.1 → kPa)
        )
        p.update(overrides)
        return p

    def test_chuncheon_april1(self):
        assert abs(eto_penman_monteith(**self._base_params()) - 1.77) < ABS_TOL_ETO

    def test_eto_nonnegative(self):
        assert eto_penman_monteith(**self._base_params()) >= 0

    def test_higher_temp_increases_eto(self):
        hot = eto_penman_monteith(**self._base_params(Tmax=30.0, Tmin=18.0))
        assert hot > eto_penman_monteith(**self._base_params())

    def test_higher_wind_increases_eto(self):
        windy = eto_penman_monteith(**self._base_params(u2=4.0))
        assert windy > eto_penman_monteith(**self._base_params())


# 5. 토양 물수지 — FAO-56 식(82)(83)(84)(85)(88)
class TestWaterBalance:
    """양토, 사과 기준(cropwater 값): FC=0.22, WP=0.10, Zr=1.0 m, p=0.50"""

    FC = 0.22
    WP = 0.10
    Zr = 1.0
    p = 0.50
    TAW = taw_mm(FC, WP, Zr)  # 120 mm
    RAW = raw_mm(p, TAW)  # 60 mm

    def test_taw(self):
        """TAW = 1000 × (FC−WP) × Zr = 120 mm [식(82)]"""
        assert self.TAW == pytest.approx(120.0)

    def test_raw(self):
        """RAW = p × TAW = 60 mm [식(83)]"""
        assert self.RAW == pytest.approx(60.0)

    @pytest.mark.parametrize(
        "Dr, expected_Ks",
        [(0, 1.00), (30, 1.00), (60, 1.00), (90, 0.50), (105, 0.25), (120, 0.00)],
    )
    def test_ks(self, Dr, expected_Ks):
        """Ks [식(84)]: Dr ≤ RAW → 1.0, Dr > RAW → (TAW−Dr)/(TAW−RAW). 하루 물수지의 Ks로 확인"""
        ks = wb_step(Dr, 0.0, 0.0, self.TAW, self.RAW)[0]
        assert abs(ks - expected_Ks) < ABS_TOL_KS

    def _wb(self, rain, eto):
        """cropwater compute_water_balance 자리: Kc = 1, 관수 없음"""
        days = [
            dict(date=dt.date(2026, 5, 1) + dt.timedelta(days=i), eto_mm=e, rain_mm=p, filled=False)
            for i, (p, e) in enumerate(zip(rain, eto, strict=True))
        ]
        return observed_water_balance(days, None, Soil(taw_mm=self.TAW, raw_mm=self.RAW))

    def test_dp_eq88_subtracts_same_day_etc(self):
        """[식(88) 원식] DP = P − ETc − Dr,i-1: Dr 20 mm에 비 50 mm, ETc 5 mm → DP 25 mm, Dr 0"""
        r = self._wb([0, 0, 0, 0, 50], [5, 5, 5, 5, 5])
        assert r[3]["dr_mm"] == pytest.approx(20.0)
        assert r[4]["dp_mm"] == pytest.approx(25.0)  # 간이식 max(P − Dr, 0)이면 30
        assert r[4]["dr_mm"] == pytest.approx(0.0)  # 간이식이면 ETc만큼(5) 남음

    def test_dp_zero_when_rain_within_deficit_plus_etc(self):
        """비가 고갈량 + 당일 ETc보다 적으면 DP = 0, Dr = 20 − 22 + 5 = 3"""
        r = self._wb([0, 0, 0, 0, 22], [5, 5, 5, 5, 5])
        assert r[4]["dp_mm"] == 0.0 and r[4]["dr_mm"] == pytest.approx(3.0)

    def test_small_rain_below_deficit(self):
        """강수 < 고갈량이면 DP = 0"""
        r = self._wb([0, 0, 0, 0, 10], [5, 5, 5, 5, 5])
        assert r[4]["dp_mm"] == 0.0 and r[4]["dr_mm"] == pytest.approx(15.0)

    def test_ks_from_previous_day_depletion(self):
        """[Ks 판단 시점] 전날 끝 고갈량으로 정함: Dr 90에 비 40 → Ks = (120−90)/60 = 0.5"""
        ks, etc_adj, dp, dr = wb_step(90.0, 40.0, 5.0, self.TAW, self.RAW)
        assert ks == pytest.approx(0.5)
        assert etc_adj == pytest.approx(2.5)
        assert dp == 0.0 and dr == pytest.approx(52.5)

    def test_wb_step_matches_observed_water_balance(self):
        """관측 물수지는 wb_step과 같은 값을 냄(무관수, Kc = 1)"""
        rain, eto = [0, 0, 30, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 12, 0], [5.0] * 17
        r = self._wb(rain, eto)
        dr = 0.0
        for rec, p, e in zip(r, rain, eto, strict=True):
            ks, _, dp, dr = wb_step(dr, p, e, self.TAW, self.RAW)
            assert rec["dr_mm"] == pytest.approx(dr) and rec["dp_mm"] == pytest.approx(dp)

    def test_irrigation_reduces_depletion(self):
        """순관수량은 비처럼 Dr을 줄임: Dr 70, 순관수 66.5, ETc 5 → Ks 0.8333"""
        ks, etc_adj, dp, dr = wb_step(70.0, 0.0, 5.0, self.TAW, self.RAW, irr_net=66.5)
        assert ks == pytest.approx(50 / 60)
        assert dp == 0.0 and dr == pytest.approx(70 - 66.5 + 5 * 50 / 60)

    def test_over_irrigation_percolates(self):
        """[식88] 관수가 고갈량 + 당일 ETc보다 많으면 남는 양은 심층침투: DP = 40 − 5 − 20 = 15"""
        ks, etc_adj, dp, dr = wb_step(20.0, 0.0, 5.0, self.TAW, self.RAW, irr_net=40.0)
        assert dp == pytest.approx(15.0) and dr == 0.0

    def test_no_irrigation_default_unchanged(self):
        """irr_net 생략(0)이면 무관수 결과와 같음"""
        assert wb_step(30.0, 12.0, 5.0, self.TAW, self.RAW) == wb_step(
            30.0, 12.0, 5.0, self.TAW, self.RAW, irr_net=0.0
        )

    def test_dr_bounds(self):
        """Dr은 0~TAW 안에 있다"""
        assert wb_step(0.0, 200.0, 5.0, self.TAW, self.RAW)[3] == 0.0
        assert wb_step(118.0, 0.0, 50.0, self.TAW, self.RAW)[3] <= self.TAW


# 6. Kc 생육단계 보간 — kc_of_date()
class TestKcCurve:
    """사과: L_ini=20, L_dev=70, L_mid=90, L_late=30 / Kc 0.50→1.20→0.95, 발아일 2026-04-01"""

    KC_KW = dict(
        start_ordinal=dt.date(2026, 4, 1).toordinal(),
        L_ini=20,
        L_dev=70,
        L_mid=90,
        L_late=30,
        kc_ini=0.50,
        kc_mid=1.20,
        kc_end=0.95,
    )

    def _kc(self, d):
        return kc_of_date(d.toordinal(), **self.KC_KW)

    def test_ini_stage(self):
        assert abs(self._kc(dt.date(2026, 4, 10)) - 0.50) < 0.001

    def test_mid_stage(self):
        assert abs(self._kc(dt.date(2026, 7, 20)) - 1.20) < 0.001

    def test_dev_monotone(self):
        kcs = [self._kc(d) for d in (dt.date(2026, 5, 1), dt.date(2026, 5, 20), dt.date(2026, 6, 10))]
        assert all(0.50 <= k <= 1.20 for k in kcs)
        assert kcs[0] < kcs[1] < kcs[2]

    def test_first_dev_day(self):
        """발육기 첫날(21일차)은 Kc_ini에서 1/70만큼 오른다 (cropwater test_kc_params_and_series)"""
        assert self._kc(dt.date(2026, 4, 21)) == pytest.approx(0.5 + (1.20 - 0.5) / 70)

    def test_before_start_and_dormancy(self):
        """발아 전·생육 종료 후: Kc = 0"""
        assert self._kc(dt.date(2026, 3, 31)) == 0.0
        assert self._kc(dt.date(2026, 11, 1)) == 0.0


def test_kc_climate_adjust():
    """FAO-56 식(62)/(65): u2 2 m/s, RHmin 45%면 보정 없음. 후기 표값 0.45 이하는 보정하지 않음"""
    assert kc_climate_adjust(1.20, 2.0, 45.0, 3.2) == pytest.approx(1.20)
    assert kc_climate_adjust(1.20, 1.5, 55, 3.2) == pytest.approx(
        1.20 + (0.04 * -0.5 - 0.004 * 10) * (3.2 / 3) ** 0.3
    )
    assert kc_climate_adjust(0.40, 4.0, 20.0, 3.2, is_end=True) == 0.40
    assert kc_climate_adjust(1.20, 10.0, 0.0, 3.2) == kc_climate_adjust(1.20, 6.0, 20.0, 3.2)  # 범위 제한


class TestMissingDays:
    """관측 결측일(cropwater #20): 빠진 날짜 행 추가 + ETo = 앞 7일 평균"""

    def test_missing_dates_interior_only(self):
        ds = [dt.date(2025, 6, d) for d in (3, 4, 5, 7, 10)]
        assert missing_dates(ds) == [dt.date(2025, 6, 6), dt.date(2025, 6, 8), dt.date(2025, 6, 9)]
        assert missing_dates([dt.date(2025, 6, 1)]) == []

    def test_prev_mean(self):
        v = [1, 2, 3, 4, 5, 6, 7, 8, None]
        assert prev_mean(v, 8) == pytest.approx(sum(range(2, 9)) / 7)
        assert prev_mean(v, 0) is None and prev_mean([None, 4], 2) == 4
