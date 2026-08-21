"""
급여·근로시간 순수 계산 로직.

프레임워크/DB 비의존 — 그래서 단위 테스트가 쉽고, 나중에 지문 단말기(ESP32)가
보낸 로그를 배치로 재계산할 때도 그대로 재사용됩니다.

기존 main.py 대비 달라진 점
  1. 휴게시간: '자동 차감'이 기본값일 뿐, 기록마다 override 로 덮어쓸 수 있음
  2. 주휴수당: 근로자별 정책(auto/always/never) + 주(ISO week) 단위 override
  3. 3.3% 사업소득 원천징수 역산(세후 → 세전) 추가
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# ─────────────────────────────────────────────────────────────
#  1. 시각 스냅 / 휴게시간
# ─────────────────────────────────────────────────────────────

SNAP_UNIT_MIN = 30      # 30분 단위로 스냅
SNAP_TOLERANCE_MIN = 10  # 슬롯 지난 지 10분 이내면 그 슬롯으로 내림


def snap_minutes(total_min: int, *, mode: str = "late") -> int:
    """분 단위 시각을 30분 슬롯으로 스냅.

    mode="late"  : 기존 main.py 동작 (슬롯 +10분 이내면 내림, 그 외엔 올림)
    mode="near"  : 가장 가까운 슬롯으로 반올림
    mode="none"  : 스냅하지 않음(실제 시각 그대로)

    ⚠️ mode="late" 를 출근에 적용하면 09:15 출근이 09:30으로 밀려
    15분이 무급 처리됩니다. 출근은 "near" 또는 "none",
    퇴근은 "late" 로 나눠 쓰는 편이 분쟁 소지가 적습니다.
    """
    if mode == "none":
        return total_min
    floor_slot = (total_min // SNAP_UNIT_MIN) * SNAP_UNIT_MIN
    ceil_slot = floor_slot + SNAP_UNIT_MIN
    if mode == "near":
        return floor_slot if (total_min - floor_slot) < SNAP_UNIT_MIN / 2 else ceil_slot
    if total_min - floor_slot <= SNAP_TOLERANCE_MIN:
        return floor_slot
    return ceil_slot


def snap_time(h: int, m: int, *, mode: str = "late") -> tuple[int, int]:
    return divmod(snap_minutes(h * 60 + m, mode=mode), 60)


def legal_break(total_min: int) -> int:
    """근로기준법 제54조 기준 최소 휴게시간(분).

    4시간 이상 → 30분, 8시간 이상 → 1시간.
    (경계값은 실제 근로시간 기준으로 판단하는 것이 원칙이라 여기서는
     총 체류시간 기준의 보수적 계산을 유지합니다.)
    """
    if total_min >= 8 * 60:
        return 60
    if total_min >= 4 * 60:
        return 30
    return 0


def resolve_break(total_min: int, override: int | None) -> int:
    """override 가 None 이면 법정 자동 계산, 아니면 지정값을 그대로 사용.

    override=0 은 '휴게 없음'이라는 명시적 의사이므로 자동 계산으로
    되돌리지 않습니다. 자동으로 돌리려면 None 을 넣으세요.
    """
    if override is None:
        return legal_break(total_min)
    return max(0, min(int(override), total_min))


@dataclass
class WorkSpan:
    total_min: int
    break_min: int
    paid_min: int
    legal_break_min: int

    @property
    def break_is_short(self) -> bool:
        """법정 기준보다 적게 잡힌 경우 — 관리자 화면에서 경고 배지를 띄우는 용도."""
        return self.break_min < self.legal_break_min


def calc_span(in_h: int, in_m: int, out_h: int, out_m: int,
              break_override: int | None = None) -> WorkSpan | None:
    """출퇴근 시각 → 근로시간. 자정을 넘기면 다음날로 간주. 역순이면 None."""
    total = (out_h * 60 + out_m) - (in_h * 60 + in_m)
    if total < 0:
        total += 24 * 60          # 야간조 대응 (22:00 → 06:00)
    if total > 20 * 60:
        return None
    # total == 0 은 오류로 막지 않습니다. 실수로 출근을 누른 사람이 퇴근을
    # 못 눌러 기록이 열린 채 남는 상황이 더 나쁘기 때문입니다(0분으로 닫고
    # 관리자가 지웁니다).
    brk = resolve_break(total, break_override)
    return WorkSpan(total, brk, total - brk, legal_break(total))


# ─────────────────────────────────────────────────────────────
#  2. 월 급여 계산
# ─────────────────────────────────────────────────────────────

REGULAR_MAX_MIN = 8 * 60
WEEKLY_HOLIDAY_MIN_HOURS = 15      # 주 15시간 이상
WEEKLY_HOLIDAY_FULL_HOURS = 40     # 40시간 기준 비례


@dataclass
class WorkerSpec:
    """계산에 필요한 근로자 정보만 담은 값 객체 (ORM 모델과 분리)."""
    name: str
    hourly: int
    contract_days: int = 5
    employment_type: str = "파트타임"
    extra_eligible: bool = False
    weekly_holiday_policy: str = "auto"   # auto | always | never
    net_pay_agreement: bool = False       # 세후 합의 지급 대상(3.3% 역산 대상)


@dataclass
class DayRecord:
    work_date: date
    paid_min: int
    break_min: int = 0
    is_holiday: bool = False


def iso_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


def calc_monthly(
    spec: WorkerSpec,
    records: list[DayRecord],          # ← 해당 근로자의 '전체' 기록을 넘기세요
    year: int,
    month: int,
    weekly_overrides: dict[tuple[int, int], bool] | None = None,
    adjustment: int = 0,
) -> dict:
    """월 급여 계산.

    records 는 월별로 잘라서 넣지 말고 전체를 넘깁니다. 주휴수당은 주 단위라
    월 경계에 걸친 주를 잘라내면 미지급/이중지급이 생기기 때문입니다.
    (귀속 기준: 해당 주의 일요일이 속한 달)
    """
    weekly_overrides = weekly_overrides or {}

    month_recs = [r for r in records
                  if r.work_date.year == year and r.work_date.month == month]

    base_pay = overtime_pay = holiday_pay = 0.0
    total_min = 0
    for r in month_recs:
        mins = r.paid_min
        total_min += mins
        normal = min(mins, REGULAR_MAX_MIN)
        extra = max(0, mins - REGULAR_MAX_MIN)

        base_pay += (mins / 60) * spec.hourly

        if spec.extra_eligible:
            if extra > 0:
                overtime_pay += (extra / 60) * spec.hourly * 0.5
            if r.is_holiday:
                holiday_pay += ((normal + extra) / 60) * spec.hourly * 0.5

    # ── 주휴수당 ──
    wk_hours: dict[tuple[int, int], float] = {}
    wk_days: dict[tuple[int, int], int] = {}
    for r in records:
        k = iso_key(r.work_date)
        wk_hours[k] = wk_hours.get(k, 0.0) + r.paid_min / 60
        wk_days[k] = wk_days.get(k, 0) + 1

    weekly_pay = 0.0
    weekly_detail = []
    for key, hours in sorted(wk_hours.items()):
        iso_yr, iso_wk = key
        sunday = datetime.fromisocalendar(iso_yr, iso_wk, 7).date()
        if not (sunday.year == year and sunday.month == month):
            continue

        auto_ok = (hours >= WEEKLY_HOLIDAY_MIN_HOURS
                   and wk_days[key] >= spec.contract_days)

        if key in weekly_overrides:                # ① 주 단위 수동 지정이 최우선
            granted, source = weekly_overrides[key], "manual"
        elif spec.weekly_holiday_policy == "never":  # ② 근로자별 정책
            granted, source = False, "policy"
        elif spec.weekly_holiday_policy == "always":
            granted, source = hours > 0, "policy"
        else:                                        # ③ 법정 자동 판정
            granted, source = auto_ok, "auto"

        amount = 0.0
        if granted:
            amount = min(hours / WEEKLY_HOLIDAY_FULL_HOURS, 1.0) * 8 * spec.hourly
            weekly_pay += amount

        weekly_detail.append({
            "iso_year": iso_yr, "iso_week": iso_wk,
            "sunday": sunday.isoformat(),
            "hours": round(hours, 2), "days": wk_days[key],
            "auto_ok": auto_ok, "granted": granted,
            "source": source, "amount": round(amount),
        })

    extra_pay = overtime_pay + holiday_pay
    total_pay = round(base_pay + extra_pay + weekly_pay) + adjustment

    return {
        "name": spec.name,
        "hourly": spec.hourly,
        "employment_type": spec.employment_type,
        "extra_eligible": spec.extra_eligible,
        "net_pay_agreement": spec.net_pay_agreement,
        "total_hours": round(total_min / 60, 2),
        "base_pay": round(base_pay),
        "overtime_pay": round(overtime_pay),
        "holiday_pay": round(holiday_pay),
        "extra_pay": round(extra_pay),
        "weekly_pay": round(weekly_pay),
        "adjustment": adjustment,
        "total_pay": total_pay,
        "weekly_detail": weekly_detail,
        "days_worked": len(month_recs),
    }


# ─────────────────────────────────────────────────────────────
#  3. 3.3% 사업소득 원천징수 역산 (세후 → 세전)
# ─────────────────────────────────────────────────────────────
#
#  ⚠️ 아래는 '실무에서 통용되는 일반적인 계산 방식'을 코드로 옮긴 것입니다.
#     원 단위 절사 여부·소액부징수 적용 여부는 사업장마다 처리가 달라
#     최종 신고 전에 반드시 세무대리인과 확인하세요.

INCOME_TAX_RATE = 0.03      # 사업소득 원천징수 소득세 3%
LOCAL_TAX_RATE = 0.10       # 지방소득세 = 소득세의 10% (= 총액의 0.3%)
ROUND_UNIT = 10             # 원 단위 절사 단위 (0 이면 절사 없음)
SMALL_AMOUNT_THRESHOLD = 1000   # 소액부징수 기준


def _floor_unit(value: float, unit: int) -> int:
    if unit <= 1:
        return int(value)
    return int(value // unit) * unit


def withhold(gross: int, *, round_unit: int = ROUND_UNIT,
             small_amount_exemption: bool = False) -> dict:
    """세전 금액 → (소득세, 지방소득세, 실지급액)."""
    income_tax = _floor_unit(gross * INCOME_TAX_RATE, round_unit)
    if small_amount_exemption and income_tax < SMALL_AMOUNT_THRESHOLD:
        income_tax = 0
    local_tax = _floor_unit(income_tax * LOCAL_TAX_RATE, round_unit)
    return {
        "gross": gross,
        "income_tax": income_tax,
        "local_tax": local_tax,
        "total_tax": income_tax + local_tax,
        "net": gross - income_tax - local_tax,
    }


def gross_from_net(net: int, *, round_unit: int = ROUND_UNIT,
                   small_amount_exemption: bool = False) -> dict:
    """세후 실지급액 → 세전 금액 역산.

    단순 공식 gross = net / 0.967 로 시작점을 잡은 뒤,
    원 단위 절사까지 반영해 실제로 그 세후액이 나오는 세전액을 탐색합니다.
    절사 때문에 정확히 떨어지지 않으면 exact=False 와 함께 근사값을 돌려줍니다.
    """
    net = int(round(net))
    if net <= 0:
        return {**withhold(0, round_unit=round_unit), "exact": True, "requested_net": net}

    approx = int(round(net / (1 - INCOME_TAX_RATE * (1 + LOCAL_TAX_RATE))))

    best = None
    for g in range(max(1, approx - 200), approx + 300):
        r = withhold(g, round_unit=round_unit,
                     small_amount_exemption=small_amount_exemption)
        if r["net"] == net:
            return {**r, "exact": True, "requested_net": net}
        diff = abs(r["net"] - net)
        if best is None or diff < best[0]:
            best = (diff, r)

    r = best[1] if best else withhold(approx, round_unit=round_unit)
    return {**r, "exact": False, "requested_net": net}


def gross_up_batch(rows: list[dict], *, amount_key: str = "total_pay",
                   **kwargs) -> list[dict]:
    """관리자 화면에서 체크된 인원 목록에 일괄 역산."""
    out = []
    for row in rows:
        calc = gross_from_net(row[amount_key], **kwargs)
        out.append({**row, "grossup": calc})
    return out
