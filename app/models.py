"""DB 모델 (SQLModel). SQLite 로 시작해서 인원이 늘면 Postgres 로 그대로 이전 가능."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


class Worker(SQLModel, table=True):
    """근로자. PIN 은 해시로만 저장합니다(원문 저장 금지)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    pin_hash: str
    hourly: int = 10030
    contract_days: int = 5
    employment_type: str = "파트타임"        # 파트타임 | 정규
    extra_eligible: bool = False             # 연장·휴일 50% 가산 적용 여부

    # ── 요구사항 3: 휴게/주휴 유동 관리 ──
    break_policy: str = "ask"                # ask | auto | none
    #   ask  : 퇴근 시 근로자에게 휴게시간을 물어봄 (권장)
    #   auto : 법정 기준으로 자동 차감
    #   none : 항상 0분 (휴게 없는 단시간 근로자)
    weekly_holiday_policy: str = "auto"      # auto | always | never

    # ── 요구사항 4: 3.3% 역산 대상 ──
    net_pay_agreement: bool = False          # 세후 금액으로 합의한 근로자

    active: bool = True
    created_at: datetime = Field(default_factory=datetime.now)


class Record(SQLModel, table=True):
    """일별 근무 기록."""
    __table_args__ = (UniqueConstraint("worker_id", "work_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    worker_id: int = Field(foreign_key="worker.id", index=True)
    work_date: date = Field(index=True)

    # 스냅된(급여 기준) 시각
    in_h: Optional[int] = None
    in_m: Optional[int] = None
    out_h: Optional[int] = None
    out_m: Optional[int] = None

    # 버튼을 실제로 누른 시각 — 감사 추적용, 급여 계산에는 미사용
    real_in_at: Optional[datetime] = None
    real_out_at: Optional[datetime] = None

    total_minutes: Optional[int] = None       # 체류시간
    break_minutes: Optional[int] = None       # 최종 적용된 휴게(분)
    break_override: Optional[int] = None      # None = 자동, 숫자 = 수동 지정
    break_source: str = "auto"                # auto | worker | admin
    minutes: Optional[int] = None             # 실근무(급여 기준)

    is_holiday: bool = False
    memo: Optional[str] = None

    # 위치 검증 흔적 (분쟁 시 근거)
    in_ip: Optional[str] = None
    out_ip: Optional[str] = None
    in_device: Optional[str] = None
    out_device: Optional[str] = None

    edited_by: Optional[str] = None
    edited_at: Optional[datetime] = None


class WeeklyHolidayOverride(SQLModel, table=True):
    """주휴수당 주(週) 단위 수동 지정. 없으면 근로자 정책 → 법정 자동 판정 순."""
    __table_args__ = (UniqueConstraint("worker_id", "iso_year", "iso_week"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    worker_id: int = Field(foreign_key="worker.id", index=True)
    iso_year: int
    iso_week: int
    granted: bool
    reason: Optional[str] = None
    edited_by: Optional[str] = None
    edited_at: datetime = Field(default_factory=datetime.now)


class PayrollAdjustment(SQLModel, table=True):
    """월 급여 화면에서 직접 수정한 가감액 (요구사항 5-①)."""
    __table_args__ = (UniqueConstraint("worker_id", "year", "month"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    worker_id: int = Field(foreign_key="worker.id", index=True)
    year: int
    month: int
    amount: int = 0
    memo: Optional[str] = None
    edited_by: Optional[str] = None
    edited_at: datetime = Field(default_factory=datetime.now)


class AdminUser(SQLModel, table=True):
    """관리자. 위치 제약 없이 어디서든 접속."""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: str = "owner"                       # owner | manager
    active: bool = True


class AppSetting(SQLModel, table=True):
    """전역 설정 보관용 key-value 테이블.

    지금은 단말기 인증 코드(kiosk_passcode) 하나만 쓰지만,
    나중에 사업장명·기본 시급 같은 설정이 늘어도 테이블 추가 없이 확장됩니다.
    """
    key: str = Field(primary_key=True)
    value: str
    updated_by: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.now)
