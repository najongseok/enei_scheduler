"""
관리자용 라우터 — 위치 제한 없음. 스마트폰·개인 PC 어디서든 접속합니다.

근로자용(/) 과 URL 네임스페이스, 의존성, 템플릿이 완전히 분리돼 있어
POS PC 에서 주소를 직접 쳐도 관리자 기능에 닿지 않습니다.
"""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..excel import build_payroll_workbook
from ..models import (AdminUser, PayrollAdjustment, Record,
                      WeeklyHolidayOverride, Worker)
from ..payroll import (DayRecord, WorkerSpec, calc_span, calc_monthly,
                       gross_from_net, iso_key)
from ..security import (current_admin, get_passcode, hash_secret,
                        require_admin, require_owner, set_passcode,
                        verify_secret)
from ..templating import render

router = APIRouter(prefix="/admin", tags=["admin"])


# ─────────────────────────────────────────────────────────────
#  데이터 조립
# ─────────────────────────────────────────────────────────────

def _spec(w: Worker) -> WorkerSpec:
    return WorkerSpec(
        name=w.name, hourly=w.hourly, contract_days=w.contract_days,
        employment_type=w.employment_type, extra_eligible=w.extra_eligible,
        weekly_holiday_policy=w.weekly_holiday_policy,
        net_pay_agreement=w.net_pay_agreement,
    )


def _day_records(db: Session, worker_id: int) -> list[DayRecord]:
    rows = db.exec(select(Record).where(Record.worker_id == worker_id)).all()
    return [DayRecord(work_date=r.work_date, paid_min=r.minutes,
                      break_min=r.break_minutes or 0, is_holiday=r.is_holiday)
            for r in rows if r.minutes is not None]


def _weekly_overrides(db: Session, worker_id: int) -> dict[tuple[int, int], bool]:
    rows = db.exec(select(WeeklyHolidayOverride)
                   .where(WeeklyHolidayOverride.worker_id == worker_id)).all()
    return {(o.iso_year, o.iso_week): o.granted for o in rows}


def _adjustment(db: Session, worker_id: int, year: int, month: int) -> PayrollAdjustment | None:
    return db.exec(select(PayrollAdjustment).where(
        PayrollAdjustment.worker_id == worker_id,
        PayrollAdjustment.year == year,
        PayrollAdjustment.month == month)).first()


def weekly_summary(rows: list[dict]) -> dict:
    """주휴수당 화면용 요약.

    모든 주를 나열하는 대신, 관리자가 실제로 눈으로 봐야 하는 주만 골라냅니다.
      · short   : 약정일에 못 미쳐 미지급되는 주  (임금 관련 문의가 나오는 지점)
      · over    : 약정일을 넘겨 일한 주            (연장·대체휴무 확인 대상)
      · manual  : 자동 판정을 관리자가 덮어쓴 주   (근거를 남겨야 하는 지점)
    나머지 '정상' 주는 건수만 셉니다.
    """
    short, over, manual, normal = [], [], [], 0
    for r in rows:
        for w in r["weekly_detail"]:
            item = {**w, "name": r["name"], "worker_id": r["worker_id"],
                    "contract_days": r["contract_days"]}
            if w["source"] == "manual":
                manual.append(item)
            elif w["days"] < r["contract_days"]:
                short.append(item)
            elif w["days"] > r["contract_days"]:
                over.append(item)
            else:
                normal += 1
    return {"short": short, "over": over, "manual": manual, "normal": normal,
            "flagged": len(short) + len(over) + len(manual)}


def payroll_rows(db: Session, year: int, month: int,
                 only_ids: list[int] | None = None) -> list[dict]:
    """월 급여 리스트. 화면·엑셀·역산이 전부 이 함수 하나를 씁니다."""
    q = select(Worker).where(Worker.active == True).order_by(Worker.name)  # noqa: E712
    workers = db.exec(q).all()
    if only_ids:
        workers = [w for w in workers if w.id in set(only_ids)]

    rows = []
    for w in workers:
        adj = _adjustment(db, w.id, year, month)
        res = calc_monthly(
            _spec(w), _day_records(db, w.id), year, month,
            weekly_overrides=_weekly_overrides(db, w.id),
            adjustment=adj.amount if adj else 0,
        )
        res["worker_id"] = w.id
        res["contract_days"] = w.contract_days
        res["adjust_memo"] = adj.memo if adj else ""
        rows.append(res)
    return rows


# ─────────────────────────────────────────────────────────────
#  로그인
# ─────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "admin_login.html",
                                      {"request": request, "error": None})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_session)):
    user = db.exec(select(AdminUser).where(AdminUser.username == username)).first()
    if user is None or not user.active or not verify_secret(password, user.password_hash):
        return render(request, "admin_login.html",
            {"request": request, "error": "아이디 또는 비밀번호가 맞지 않습니다."},
            status_code=401)
    request.session["admin_id"] = user.id
    return RedirectResponse("/admin/payroll", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.pop("admin_id", None)
    return RedirectResponse("/admin/login", status_code=303)


# ─────────────────────────────────────────────────────────────
#  월 급여 대시보드 (요구사항 4·5)
# ─────────────────────────────────────────────────────────────

@router.get("/payroll", response_class=HTMLResponse)
def payroll_page(request: Request, year: int | None = None, month: int | None = None,
                 admin: AdminUser = Depends(require_admin),
                 db: Session = Depends(get_session)):
    today = date.today()
    year, month = year or today.year, month or today.month
    rows = payroll_rows(db, year, month)
    return render(request, "admin_payroll.html", {
        "request": request, "admin": admin,
        "year": year, "month": month, "rows": rows,
        "weekly": weekly_summary(rows),
        "total_sum": sum(r["total_pay"] for r in rows),
    })


@router.post("/payroll/adjust", response_class=HTMLResponse)
def save_adjustment(request: Request, worker_id: int = Form(...),
                    year: int = Form(...), month: int = Form(...),
                    amount: int = Form(0), memo: str = Form(""),
                    admin: AdminUser = Depends(require_admin),
                    db: Session = Depends(get_session)):
    """리스트에서 직접 고친 가감액 저장 (다운로드 전에 화면에서 수정)."""
    adj = _adjustment(db, worker_id, year, month)
    if adj is None:
        adj = PayrollAdjustment(worker_id=worker_id, year=year, month=month)
    adj.amount = amount
    adj.memo = memo
    adj.edited_by = admin.username
    adj.edited_at = datetime.now()
    db.add(adj)
    db.commit()

    row = payroll_rows(db, year, month, only_ids=[worker_id])[0]
    return render(request, "_payroll_row.html", {
        "request": request, "r": row, "year": year, "month": month})


@router.post("/payroll/grossup", response_class=HTMLResponse)
def grossup(request: Request,
            year: int = Form(...), month: int = Form(...),
            worker_ids: list[int] = Form(default=[]),
            round_unit: int = Form(10),
            admin: AdminUser = Depends(require_admin),
            db: Session = Depends(get_session)):
    """요구사항 4 — 체크된 인원의 세후 지급액에서 3.3% 를 역산해 세전액 표기."""
    if not worker_ids:
        return HTMLResponse('<p class="empty">역산할 인원을 먼저 선택하세요.</p>')

    rows = payroll_rows(db, year, month, only_ids=worker_ids)
    result = []
    for r in rows:
        # 역산은 '세후합의' 지정된 직원만. 나머지는 계산하지 않고 그대로 둡니다.
        calc = (gross_from_net(r["total_pay"], round_unit=round_unit)
                if r["net_pay_agreement"] else None)
        result.append({**r, "g": calc})

    eligible = [x for x in result if x["g"]]
    skipped = [x for x in result if not x["g"]]

    return render(request, "_grossup.html", {
        "request": request, "rows": eligible, "skipped": skipped,
        "year": year, "month": month,
        "sum_gross": sum(x["g"]["gross"] for x in eligible),
        "sum_net": sum(x["total_pay"] for x in eligible),
        "sum_tax": sum(x["g"]["total_tax"] for x in eligible),
    })


@router.post("/payroll/export")
def export_selected(year: int = Form(...), month: int = Form(...),
                    worker_ids: list[int] = Form(default=[]),
                    include_grossup: bool = Form(False),
                    admin: AdminUser = Depends(require_admin),
                    db: Session = Depends(get_session)):
    """요구사항 5-② — 체크된 인원만 엑셀로 내보내기."""
    if not worker_ids:
        raise HTTPException(400, "내보낼 인원을 선택하세요.")

    rows = payroll_rows(db, year, month, only_ids=worker_ids)
    wb = build_payroll_workbook(rows, year, month, include_grossup=include_grossup)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    # 한글 파일명은 RFC 5987 방식으로 인코딩해야 헤더에 실을 수 있습니다.
    ascii_name = f"payroll_{year}{month:02d}.xlsx"
    utf8_name = quote(f"급여지급리스트_{year}{month:02d}.xlsx")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"})


# ─────────────────────────────────────────────────────────────
#  근무기록 수정 (요구사항 3 — 관리자 쪽 조정)
# ─────────────────────────────────────────────────────────────

@router.get("/records", response_class=HTMLResponse)
def records_page(request: Request, year: int | None = None, month: int | None = None,
                 worker_id: int | None = None,
                 admin: AdminUser = Depends(require_admin),
                 db: Session = Depends(get_session)):
    today = date.today()
    year, month = year or today.year, month or today.month

    q = select(Record).order_by(Record.work_date.desc())
    if worker_id:
        q = q.where(Record.worker_id == worker_id)
    rows = [r for r in db.exec(q).all()
            if r.work_date.year == year and r.work_date.month == month]

    workers = {w.id: w for w in db.exec(select(Worker)).all()}
    return render(request, "admin_records.html", {
        "request": request, "admin": admin, "rows": rows, "workers": workers,
        "year": year, "month": month, "worker_id": worker_id})


@router.post("/records/{record_id}", response_class=HTMLResponse)
def edit_record(record_id: int, request: Request,
                in_h: int = Form(...), in_m: int = Form(...),
                out_h: int = Form(...), out_m: int = Form(...),
                break_mode: str = Form("auto"),       # auto | manual
                break_minutes: int = Form(0),
                is_holiday: bool = Form(False),
                memo: str = Form(""),
                admin: AdminUser = Depends(require_admin),
                db: Session = Depends(get_session)):
    rec = db.get(Record, record_id)
    if rec is None:
        raise HTTPException(404, "기록을 찾을 수 없습니다.")

    override = None if break_mode == "auto" else max(0, break_minutes)
    span = calc_span(in_h, in_m, out_h, out_m, break_override=override)
    if span is None:
        raise HTTPException(400, "퇴근 시각이 출근 시각보다 이릅니다.")

    rec.in_h, rec.in_m, rec.out_h, rec.out_m = in_h, in_m, out_h, out_m
    rec.total_minutes = span.total_min
    rec.break_minutes = span.break_min
    rec.break_override = override
    rec.break_source = "auto" if override is None else "admin"
    rec.minutes = span.paid_min
    rec.is_holiday = is_holiday
    rec.memo = memo
    rec.edited_by = admin.username
    rec.edited_at = datetime.now()
    db.add(rec)
    db.commit()
    db.refresh(rec)

    worker = db.get(Worker, rec.worker_id)
    return render(request, "_record_row.html", {
        "request": request, "r": rec, "w": worker})


# ─────────────────────────────────────────────────────────────
#  주휴수당 주 단위 토글 (요구사항 3)
# ─────────────────────────────────────────────────────────────

@router.post("/weekly-holiday", response_class=HTMLResponse)
def toggle_weekly(request: Request,
                  worker_id: int = Form(...),
                  iso_year: int = Form(...), iso_week: int = Form(...),
                  granted: bool = Form(...), reason: str = Form(""),
                  reset: bool = Form(False),
                  admin: AdminUser = Depends(require_admin),
                  db: Session = Depends(get_session)):
    row = db.exec(select(WeeklyHolidayOverride).where(
        WeeklyHolidayOverride.worker_id == worker_id,
        WeeklyHolidayOverride.iso_year == iso_year,
        WeeklyHolidayOverride.iso_week == iso_week)).first()

    if reset:                       # 수동 지정 해제 → 법정 자동 판정으로 복귀
        if row:
            db.delete(row)
            db.commit()
        return HTMLResponse('<span class="badge badge-auto">자동</span>')

    if row is None:
        row = WeeklyHolidayOverride(worker_id=worker_id, iso_year=iso_year,
                                    iso_week=iso_week, granted=granted)
    row.granted = granted
    row.reason = reason
    row.edited_by = admin.username
    row.edited_at = datetime.now()
    db.add(row)
    db.commit()

    label = "지급" if granted else "미지급"
    cls = "badge-on" if granted else "badge-off"
    return HTMLResponse(f'<span class="badge {cls}">{label} (수동)</span>')


# ─────────────────────────────────────────────────────────────
#  직원 관리 — 여기서 휴게/주휴 '기본 정책'과 세후합의 여부를 정합니다
# ─────────────────────────────────────────────────────────────

@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request, admin: AdminUser = Depends(require_admin),
                 db: Session = Depends(get_session)):
    workers = db.exec(select(Worker).order_by(Worker.name)).all()
    return render(request, "admin_workers.html", {
        "request": request, "admin": admin, "workers": workers})


@router.post("/workers")
def create_worker(name: str = Form(...), pin: str = Form(...),
                  hourly: int = Form(10030), contract_days: int = Form(5),
                  employment_type: str = Form("파트타임"),
                  extra_eligible: bool = Form(False),
                  break_policy: str = Form("ask"),
                  weekly_holiday_policy: str = Form("auto"),
                  net_pay_agreement: bool = Form(False),
                  admin: AdminUser = Depends(require_owner),
                  db: Session = Depends(get_session)):
    if db.exec(select(Worker).where(Worker.name == name)).first():
        raise HTTPException(400, "이미 등록된 이름입니다.")
    db.add(Worker(name=name, pin_hash=hash_secret(pin.strip()), hourly=hourly,
                  contract_days=contract_days, employment_type=employment_type,
                  extra_eligible=extra_eligible, break_policy=break_policy,
                  weekly_holiday_policy=weekly_holiday_policy,
                  net_pay_agreement=net_pay_agreement))
    db.commit()
    return RedirectResponse("/admin/workers", status_code=303)


@router.post("/workers/{worker_id}")
def update_worker(worker_id: int,
                  hourly: int = Form(...), contract_days: int = Form(...),
                  employment_type: str = Form(...),
                  extra_eligible: bool = Form(False),
                  break_policy: str = Form("ask"),
                  weekly_holiday_policy: str = Form("auto"),
                  net_pay_agreement: bool = Form(False),
                  active: bool = Form(True),
                  pin: str = Form(""),
                  admin: AdminUser = Depends(require_owner),
                  db: Session = Depends(get_session)):
    w = db.get(Worker, worker_id)
    if w is None:
        raise HTTPException(404, "직원을 찾을 수 없습니다.")
    w.hourly = hourly
    w.contract_days = contract_days
    w.employment_type = employment_type
    w.extra_eligible = extra_eligible
    w.break_policy = break_policy
    w.weekly_holiday_policy = weekly_holiday_policy
    w.net_pay_agreement = net_pay_agreement
    w.active = active
    if pin.strip():
        w.pin_hash = hash_secret(pin.strip())
    db.add(w)
    db.commit()
    return RedirectResponse("/admin/workers", status_code=303)


# ─────────────────────────────────────────────────────────────
#  설정 — 단말기 인증 코드
# ─────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: bool = False,
                  admin: AdminUser = Depends(require_admin),
                  db: Session = Depends(get_session)):
    return render(request, "admin_settings.html", {
        "request": request, "admin": admin,
        "passcode": get_passcode(db), "saved": saved})


@router.post("/settings/passcode")
def update_passcode(passcode: str = Form(...),
                    admin: AdminUser = Depends(require_owner),
                    db: Session = Depends(get_session)):
    """코드를 바꾸면 기존에 인증된 모든 기기가 한꺼번에 로그아웃됩니다.

    쿠키가 코드로부터 파생되기 때문인데, 직원이 그만뒀을 때 전 기기를
    한 번에 끊는 수단으로 쓸 수 있습니다.
    """
    set_passcode(db, passcode, by=admin.username)
    return RedirectResponse("/admin/settings?saved=1", status_code=303)
