"""
ENEI Scheduler Web — 앱 조립부.

라우팅 구조 (권한 분리의 뼈대)
    /                  근로자 키오스크        require_kiosk  (현장 단말기에서만)
    /api/*             근로자 API            require_kiosk + require_worker
    /admin/*           관리자 대시보드        require_admin  (기기 제한 없음)
    /kiosk-login       단말기 인증 코드 입력
"""
from __future__ import annotations

import sys
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, select

from .config import settings
from .db import engine, get_session, init_db
from .models import AdminUser, Worker
from .routers import admin as admin_router
from .routers import kiosk as kiosk_router
from .security import KioskAuthRequired, hash_secret
from .templating import render

app = FastAPI(title="ENEI Scheduler Web", docs_url=None, redoc_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="enei_session",
    https_only=settings.secure_cookies,
    same_site="lax",
    max_age=60 * 60 * 12,
)


@app.on_event("startup")
def on_startup():
    init_db()
    # 최초 실행 시 관리자 계정 자동 생성
    import os
    from sqlmodel import Session, select
    admin_id = os.getenv("INIT_ADMIN_ID")
    admin_pw = os.getenv("INIT_ADMIN_PW")
    if admin_id and admin_pw:
        with Session(engine) as db:
            existing = db.exec(select(AdminUser).where(AdminUser.username == admin_id)).first()
            if not existing:
                db.add(AdminUser(username=admin_id,
                                 password_hash=hash_secret(admin_pw),
                                 role="owner"))
                db.commit()


# ── 오류를 사람 말로 ──────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    wants_html = "text/html" in request.headers.get("accept", "")

    # 미인증 기기가 화면을 열려고 하면 인증 코드 페이지로 보냅니다.
    # (API 호출이면 아래 JSON 401 로 떨어져 프런트가 알아서 처리합니다.)
    if isinstance(exc, KioskAuthRequired) and wants_html:
        return RedirectResponse("/kiosk-login", status_code=303)

    if exc.status_code == 401 and request.url.path.startswith("/admin") and wants_html:
        return RedirectResponse("/admin/login", status_code=303)

    if wants_html:
        return render(request, "error.html",
            {"request": request, "code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code)

    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.include_router(admin_router.router)
app.include_router(kiosk_router.router)   # "/" 를 잡으므로 마지막에


# ── 최초 세팅용 CLI ───────────────────────────────────────

def seed():
    """python -m app.main seed <관리자ID> <비밀번호>"""
    if len(sys.argv) < 4:
        print("사용법: python -m app.main seed <username> <password>")
        return
    init_db()
    with Session(engine) as db:
        if db.exec(select(AdminUser).where(AdminUser.username == sys.argv[2])).first():
            print("이미 있는 관리자입니다.")
            return
        db.add(AdminUser(username=sys.argv[2],
                         password_hash=hash_secret(sys.argv[3]),
                         role="owner"))
        db.commit()
        print(f"관리자 '{sys.argv[2]}' 생성 완료. /admin/login 에서 로그인하세요.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        seed()
