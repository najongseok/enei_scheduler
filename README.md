# ENEI Scheduler Web

기존 `main.py`(CustomTkinter 근태관리) + `report.py`(급여 리포트/엑셀)를
FastAPI 기반 웹앱으로 이관한 버전입니다.

## 구조

```
app/
  payroll.py      급여·근로시간 순수 계산 로직 (프레임워크 비의존)
  models.py       DB 모델 (SQLModel) — Worker, Record, WeeklyHolidayOverride,
                  PayrollAdjustment, AdminUser, AppSetting
  security.py     RBAC + 단말기 인증 코드 + 세션
  config.py       환경설정 (.env 로 주입)
  db.py           DB 엔진/세션
  excel.py        선택 인원만 담는 엑셀 생성 (3.3% 역산 열 포함 옵션)
  templating.py   Jinja2 렌더 헬퍼
  routers/
    kiosk.py      근로자용 — 출근/퇴근/본인기록만 + 단말기 인증(/kiosk-login)
    admin.py      관리자용 — 급여/근무기록/직원/설정. 기기 제한 없음.
  templates/       kiosk.html(POS용 키오스크 UI), admin_*.html(관리자 대시보드)
migrate_from_json.py   기존 data.json → DB 이관 스크립트
smoke_test.py           전체 흐름 자동 검증 스크립트
```

## 실행

```bash
pip install -r requirements.txt

# 1. 최초 관리자 계정 생성
python -m app.main seed owner "원하는비밀번호"

# 2. 서버 실행
uvicorn app.main:app --host 0.0.0.0 --port 8000

# (선택) 기존 data.json 이관
python migrate_from_json.py /path/to/data.json
```

운영 배포 전 `.env` 로 아래 값을 반드시 바꾸세요:
- `SECRET_KEY` — 세션 서명 키
- `SECURE_COOKIES=1` — HTTPS 로 서비스할 때 (Geolocation 은 HTTPS 필수)
- `TRUST_PROXY=1` — Nginx/Cloudflare 뒤에 있을 때만

## 첫 사용 흐름

1. `/admin/login` 으로 로그인 → `/admin/settings` 에서 **단말기 인증 코드**를 확인/변경
   (첫 실행 시 무작위 6자리가 자동 생성됩니다)
2. 매장 POS PC 브라우저에서 출퇴근 주소(`/`)에 접속 → 인증 코드 입력 화면이 뜨면
   위 코드를 한 번 입력. 그 브라우저에 2년짜리 인증이 저장되고, 이후에는 바로 사용됩니다.
3. `/admin/workers` 에서 직원 등록 — 이때 "휴게 기본값"을 사람마다 정합니다
   (권장: **퇴근 시 물어봄**), 3.3% 역산이 필요한 직원은 **세후합의** 체크
4. 근로자는 POS PC(`/`)에서 고유번호로 출근/퇴근, 관리자는 폰/PC 어디서든 `/admin/payroll`

인증 코드를 바꾸면 **기존에 인증된 모든 기기가 한꺼번에 로그아웃**됩니다.
쓰지 않는 기기의 인증을 한 번에 끊고 싶을 때 쓰세요.

## 검증

```bash
python smoke_test.py
```
로그인 → 인증 코드 설정 → 미인증 리다이렉트/오답 거부 → 출퇴근 → 급여 조회 →
조정액 저장 → 3.3% 역산(대상/비대상 구분) → 선택 인원 엑셀 → 주휴수당 토글 →
관리자 휴게시간 override → 순수 로직(역산/휴게/야간조) 까지 자동 검증합니다.

## 세무 관련 주의

3.3% 역산은 통용되는 계산 방식을 코드로 옮긴 것이며, 원 단위 절사·소액부징수
적용 여부는 사업장마다 다릅니다. 신고 전 반드시 세무대리인 확인이 필요합니다.
