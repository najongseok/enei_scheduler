"""
권한(RBAC) + 단말기 인증.

단말기 인증 방식 (기존 IP/토큰 방식을 대체)
  1. 새 기기에서 근로자 페이지에 접속 → 인증 쿠키가 없으므로 /kiosk-login 으로 이동
  2. 관리자가 정해둔 '단말기 인증 코드'를 한 번 입력
  3. 그 브라우저에 2년짜리 HttpOnly 쿠키가 발급되고 출퇴근 화면으로 진입
  4. 이후 그 POS PC 에서는 코드 입력 없이 바로 사용

관리자 라우터(/admin/*)는 이 검사를 아예 거치지 않아 어디서든 접속됩니다.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from .config import settings
from .db import get_session
from .models import AdminUser, AppSetting, Worker

KIOSK_COOKIE = "kiosk_auth"
KIOSK_COOKIE_MAX_AGE = 60 * 60 * 24 * 730     # 2년
PASSCODE_KEY = "kiosk_passcode"
WORKER_SESSION_MINUTES = 3                     # 근로자 세션 자동 만료


# ─────────────────────────────────────────────────────────────
#  해시 유틸 (PIN / 관리자 비밀번호)
# ─────────────────────────────────────────────────────────────

def hash_secret(raw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"


def verify_secret(raw: str, stored: str) -> bool:
    try:
        salt, hexdigest = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt.encode(), 200_000)
    return hmac.compare_digest(dk.hex(), hexdigest)


def client_ip(request: Request) -> str:
    """리버스 프록시 뒤에서도 실제 IP 를 얻습니다 (기록 보관·시도 제한용)."""
    if settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


# ─────────────────────────────────────────────────────────────
#  단말기 인증 코드
# ─────────────────────────────────────────────────────────────

def get_passcode(db: Session) -> str:
    """현재 인증 코드. 없으면 무작위 6자리를 만들어 저장합니다."""
    row = db.get(AppSetting, PASSCODE_KEY)
    if row is None:
        row = AppSetting(key=PASSCODE_KEY,
                         value=f"{secrets.randbelow(900000) + 100000}")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row.value


def set_passcode(db: Session, code: str, by: str | None = None) -> str:
    code = code.strip()
    if not (4 <= len(code) <= 12):
        raise HTTPException(400, "인증 코드는 4~12자로 정해주세요.")
    row = db.get(AppSetting, PASSCODE_KEY)
    if row is None:
        row = AppSetting(key=PASSCODE_KEY, value=code)
    row.value = code
    row.updated_by = by
    row.updated_at = datetime.now()
    db.add(row)
    db.commit()
    return code


def _cookie_value(code: str) -> str:
    """쿠키에는 코드 원문 대신 서버 키로 서명한 값을 담습니다.

    쿠키를 들여다봐도 코드 자체는 알 수 없고, 관리자가 코드를 바꾸면
    기존 쿠키가 한꺼번에 무효가 됩니다(전 기기 재인증).
    """
    return hmac.new(settings.secret_key.encode(), code.encode(),
                    hashlib.sha256).hexdigest()


def issue_kiosk_cookie(response, code: str) -> None:
    response.set_cookie(
        KIOSK_COOKIE, _cookie_value(code),
        max_age=KIOSK_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax")


# ── 코드 입력 시도 제한 (무차별 대입 방지) ──
_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS, WINDOW_SEC = 10, 300


def _too_many_attempts(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < WINDOW_SEC]
    _attempts[ip] = hits
    return len(hits) >= MAX_ATTEMPTS


def verify_passcode(db: Session, ip: str, code: str) -> bool:
    if _too_many_attempts(ip):
        raise HTTPException(429, "시도가 너무 많습니다. 5분 뒤에 다시 시도해 주세요.")
    ok = hmac.compare_digest(code.strip(), get_passcode(db))
    if not ok:
        _attempts.setdefault(ip, []).append(time.time())
    return ok


# ─────────────────────────────────────────────────────────────
#  의존성
# ─────────────────────────────────────────────────────────────

class KioskAuthRequired(HTTPException):
    """미인증 기기. main.py 의 핸들러가 /kiosk-login 으로 보냅니다."""

    def __init__(self, detail: str = "단말기 인증이 필요합니다."):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def require_kiosk(request: Request, db: Session = Depends(get_session)) -> None:
    """근로자용 라우터 전체에 걸리는 단말기 인증 검사."""
    cookie = request.cookies.get(KIOSK_COOKIE)
    if not cookie or not hmac.compare_digest(cookie, _cookie_value(get_passcode(db))):
        raise KioskAuthRequired()


def current_admin(request: Request,
                  db: Session = Depends(get_session)) -> AdminUser | None:
    uid = request.session.get("admin_id")
    if not uid:
        return None
    admin = db.get(AdminUser, uid)
    return admin if admin and admin.active else None


def require_admin(request: Request,
                  db: Session = Depends(get_session)) -> AdminUser:
    """관리자 전용. 기기 제약 없음 — 스마트폰/개인 PC 어디서든 통과."""
    admin = current_admin(request, db)
    if admin is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="관리자 로그인이 필요합니다.")
    return admin


def require_owner(admin: AdminUser = Depends(require_admin)) -> AdminUser:
    """최고 관리자만: 시급 변경, 인증 코드 변경, 직원 추가 등."""
    if admin.role != "owner":
        raise HTTPException(status_code=403, detail="최고 관리자만 가능한 작업입니다.")
    return admin


def require_worker(request: Request,
                   db: Session = Depends(get_session)) -> Worker:
    """근로자 세션. PIN 인증 후 3분간만 유효합니다."""
    wid = request.session.get("worker_id")
    started = request.session.get("worker_at")
    if not wid or not started:
        raise HTTPException(status_code=401, detail="번호를 다시 입력해 주세요.")
    if datetime.fromisoformat(started) + timedelta(minutes=WORKER_SESSION_MINUTES) < datetime.now():
        request.session.pop("worker_id", None)
        request.session.pop("worker_at", None)
        raise HTTPException(status_code=401,
                            detail="시간이 지나 자동으로 로그아웃됐어요. 번호를 다시 입력해 주세요.")
    worker = db.get(Worker, wid)
    if worker is None or not worker.active:
        raise HTTPException(status_code=401, detail="사용할 수 없는 계정입니다.")
    return worker


def start_worker_session(request: Request, worker: Worker) -> None:
    request.session["worker_id"] = worker.id
    request.session["worker_at"] = datetime.now().isoformat()
