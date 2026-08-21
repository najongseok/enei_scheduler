"""
근로자용 라우터 — 출퇴근 단말기 전용.

이 라우터에 걸린 기능은 딱 3가지뿐입니다: 출근 / 퇴근 / 본인 기록 조회.
급여 금액, 시급, 다른 사람 정보는 응답 스키마 자체에 존재하지 않습니다.
(권한 분리는 화면을 숨기는 게 아니라 '데이터를 내보내지 않는 것'으로 합니다.)

기기 인증은 단말기 인증 코드 1회 입력 → 2년짜리 쿠키 방식입니다.
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import Record, Worker
from ..payroll import calc_span, snap_time
from ..security import (client_ip, get_passcode, issue_kiosk_cookie,
                        require_kiosk, require_worker, start_worker_session,
                        verify_passcode, verify_secret)
from ..templating import render

router = APIRouter(tags=["kiosk"])


# ── 스키마 ────────────────────────────────────────────────

class PinIn(BaseModel):
    pin: str


class ClockOut(BaseModel):
    break_minutes: int | None = None   # None = 자동(법정) 적용


def _today() -> date:
    return datetime.now().date()


def _today_record(db: Session, worker_id: int) -> Record | None:
    return db.exec(
        select(Record).where(Record.worker_id == worker_id,
                             Record.work_date == _today())
    ).first()


def _state(worker: Worker, rec: Record | None) -> dict:
    """근로자에게 내보내도 되는 정보만 담습니다 (급여·시급 없음)."""
    if rec is None:
        status = "not_in"
    elif rec.out_h is None:
        status = "working"
    else:
        status = "done"

    payload = {
        "name": worker.name,
        "status": status,
        "break_policy": worker.break_policy,
        "in_time": f"{rec.in_h:02d}:{rec.in_m:02d}" if rec and rec.in_h is not None else None,
        "out_time": f"{rec.out_h:02d}:{rec.out_m:02d}" if rec and rec.out_h is not None else None,
        "break_minutes": rec.break_minutes if rec else None,
        "work_minutes": rec.minutes if rec else None,
    }
    if rec and rec.in_h is not None and rec.out_h is None:
        now = datetime.now()
        sh, sm = snap_time(now.hour, now.minute, mode=settings.snap_out_mode)
        span = calc_span(rec.in_h, rec.in_m, sh, sm)
        if span:
            payload["suggested_break"] = (
                0 if worker.break_policy == "none" else span.legal_break_min)
            payload["elapsed_minutes"] = span.total_min
    return payload


# ── 단말기 인증 코드 ──────────────────────────────────────

@router.get("/kiosk-login")
def kiosk_login_page(request: Request):
    return render(request, "kiosk_login.html", {"error": None})


@router.post("/kiosk-login")
def kiosk_login(request: Request, passcode: str = Form(...),
                db: Session = Depends(get_session)):
    if not verify_passcode(db, client_ip(request), passcode):
        return render(request, "kiosk_login.html",
                      {"error": "인증 코드가 맞지 않습니다."}, status_code=401)

    resp = RedirectResponse("/", status_code=303)
    issue_kiosk_cookie(resp, get_passcode(db))
    return resp


# ── 화면 ──────────────────────────────────────────────────

@router.get("/")
def kiosk_page(request: Request, _: None = Depends(require_kiosk)):
    return render(request, "kiosk.html", {})


# ── PIN 인증 ──────────────────────────────────────────────

@router.post("/api/login")
def login(body: PinIn, request: Request,
          _: None = Depends(require_kiosk),
          db: Session = Depends(get_session)):
    pin = body.pin.strip()
    if not pin:
        return JSONResponse({"detail": "번호를 입력해 주세요."}, status_code=400)

    # PIN 은 해시로만 저장되므로 전수 비교 (직원 수십 명 규모에서는 충분)
    for worker in db.exec(select(Worker).where(Worker.active == True)).all():  # noqa: E712
        if verify_secret(pin, worker.pin_hash):
            start_worker_session(request, worker)
            return _state(worker, _today_record(db, worker.id))

    return JSONResponse({"detail": "등록되지 않은 번호예요."}, status_code=404)


@router.post("/api/logout")
def logout(request: Request):
    request.session.pop("worker_id", None)
    request.session.pop("worker_at", None)
    return {"ok": True}


@router.get("/api/me")
def me(worker: Worker = Depends(require_worker),
       _: None = Depends(require_kiosk),
       db: Session = Depends(get_session)):
    return _state(worker, _today_record(db, worker.id))


# ── 출근 ──────────────────────────────────────────────────

@router.post("/api/clock-in")
def clock_in(request: Request,
             worker: Worker = Depends(require_worker),
             _: None = Depends(require_kiosk),
             db: Session = Depends(get_session)):
    if _today_record(db, worker.id):
        return JSONResponse({"detail": "오늘은 이미 출근 처리됐어요."}, status_code=409)

    now = datetime.now()
    sh, sm = snap_time(now.hour, now.minute, mode=settings.snap_in_mode)
    rec = Record(
        worker_id=worker.id, work_date=now.date(),
        in_h=sh, in_m=sm, real_in_at=now,
        in_ip=client_ip(request),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return _state(worker, rec)


# ── 퇴근 (여기서 휴게시간을 물어봅니다) ────────────────────

@router.post("/api/clock-out")
def clock_out(body: ClockOut, request: Request,
              worker: Worker = Depends(require_worker),
              _: None = Depends(require_kiosk),
              db: Session = Depends(get_session)):
    rec = _today_record(db, worker.id)
    if rec is None:
        return JSONResponse({"detail": "오늘 출근 기록이 없어요."}, status_code=409)
    if rec.out_h is not None:
        return JSONResponse({"detail": "오늘은 이미 퇴근 처리됐어요."}, status_code=409)

    now = datetime.now()
    sh, sm = snap_time(now.hour, now.minute, mode=settings.snap_out_mode)

    if worker.break_policy == "auto":
        override, source = None, "auto"
    elif worker.break_policy == "none":
        override, source = 0, "worker"
    else:                                   # "ask" — 화면에서 고른 값
        override = body.break_minutes
        source = "worker" if body.break_minutes is not None else "auto"

    span = calc_span(rec.in_h, rec.in_m, sh, sm, break_override=override)
    if span is None:
        return JSONResponse({"detail": "근무시간을 계산할 수 없어요. 관리자에게 알려주세요."},
                            status_code=400)

    rec.out_h, rec.out_m = sh, sm
    rec.real_out_at = now
    rec.total_minutes = span.total_min
    rec.break_minutes = span.break_min
    rec.break_override = override
    rec.break_source = source
    rec.minutes = span.paid_min
    rec.out_ip = client_ip(request)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return _state(worker, rec)


# ── 본인 기록 조회 ────────────────────────────────────────

@router.get("/api/my-records")
def my_records(year: int | None = None, month: int | None = None,
               worker: Worker = Depends(require_worker),
               _: None = Depends(require_kiosk),
               db: Session = Depends(get_session)):
    today = _today()
    year, month = year or today.year, month or today.month

    rows = db.exec(
        select(Record).where(Record.worker_id == worker.id).order_by(Record.work_date.desc())
    ).all()
    rows = [r for r in rows if r.work_date.year == year and r.work_date.month == month]

    total = sum(r.minutes or 0 for r in rows)
    return {
        "year": year, "month": month,
        "total_hours": round(total / 60, 2),
        "days": len(rows),
        # 금액은 의도적으로 제외 — 근로자 화면에는 시간만 보여줍니다
        "records": [{
            "date": r.work_date.isoformat(),
            "in": f"{r.in_h:02d}:{r.in_m:02d}" if r.in_h is not None else "–",
            "out": f"{r.out_h:02d}:{r.out_m:02d}" if r.out_h is not None else "–",
            "break": r.break_minutes,
            "minutes": r.minutes,
            "is_holiday": r.is_holiday,
        } for r in rows],
    }
