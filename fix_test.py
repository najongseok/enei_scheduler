"""이번 요청분 검증: 9시간 가산 / 기록 추가·삭제 / 휴게 차감안함 / 시간대."""
import os
os.environ["DATABASE_URL"] = "sqlite:///./fix.db"
os.environ["SECRET_KEY"] = "test-key"
if os.path.exists("fix.db"):
    os.remove("fix.db")

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.db import engine, init_db
from app.models import AdminUser, Record, Worker
from app.security import hash_secret
from app.payroll import (DayRecord, WorkerSpec, calc_monthly, REGULAR_MAX_MIN)

KST = ZoneInfo("Asia/Seoul")

init_db()
with Session(engine) as db:
    db.add(AdminUser(username="owner", password_hash=hash_secret("pw"), role="owner"))
    db.commit()

admin = TestClient(app)
admin.post("/admin/login", data={"username": "owner", "password": "pw"})
admin.post("/admin/settings/passcode", data={"passcode": "481502"})

# ─────────────────────────────────────────────
print("[1] 가산수당 9시간 기준")
assert REGULAR_MAX_MIN == 9 * 60, REGULAR_MAX_MIN

spec = WorkerSpec(name="테스트", hourly=10000, extra_eligible=True,
                  weekly_holiday_policy="never")
today = date(2026, 9, 7)          # 월요일
for paid, label in [(8 * 60, "8시간"), (9 * 60, "9시간"), (10 * 60, "10시간")]:
    res = calc_monthly(spec, [DayRecord(work_date=today, paid_min=paid)], 2026, 9)
    print(f"  {label:>5} 근무 → 기본급 {res['base_pay']:>7,}원 / 가산 {res['overtime_pay']:>6,}원")
assert calc_monthly(spec, [DayRecord(work_date=today, paid_min=9*60)], 2026, 9)["overtime_pay"] == 0, \
    "9시간에 가산이 붙으면 안 됨"
r10 = calc_monthly(spec, [DayRecord(work_date=today, paid_min=10*60)], 2026, 9)
assert r10["overtime_pay"] == 5000, r10["overtime_pay"]   # 1시간 × 10000 × 0.5
print("  ✓ 9시간 초과분만 가산 적용")

# ─────────────────────────────────────────────
print("\n[2] 직원 등록 — 휴게 차감안함(none)")
admin.post("/admin/workers", data={
    "name": "무휴게", "pin": "1111", "hourly": "10030", "contract_days": "5",
    "employment_type": "파트타임", "break_policy": "none",
    "weekly_holiday_policy": "auto"})
admin.post("/admin/workers", data={
    "name": "물어봄", "pin": "2222", "hourly": "10030", "contract_days": "5",
    "employment_type": "파트타임", "break_policy": "ask",
    "weekly_holiday_policy": "auto"})

with Session(engine) as db:
    w_none = db.exec(select(Worker).where(Worker.name == "무휴게")).first()
    w_ask = db.exec(select(Worker).where(Worker.name == "물어봄")).first()
    assert w_none.break_policy == "none", w_none.break_policy
    assert w_ask.break_policy == "ask", w_ask.break_policy
    nid, aid = w_none.id, w_ask.id
print("  ✓ break_policy 가 DB 에 제대로 저장됨")

pos = TestClient(app)
pos.post("/kiosk-login", data={"passcode": "481502"})

s = pos.post("/api/login", json={"pin": "1111"}).json()
assert s["break_policy"] == "none", s
print("  ✓ 키오스크 응답에 break_policy='none' 전달 → 화면이 휴게 질문을 건너뜀")

s2 = pos.post("/api/login", json={"pin": "2222"}).json()
assert s2["break_policy"] == "ask", s2
print("  ✓ 'ask' 직원은 기존대로 질문")

# none 직원이 퇴근하면 휴게 0분으로 기록되는지
pos.post("/api/login", json={"pin": "1111"})
pos.post("/api/clock-in")
out = pos.post("/api/clock-out", json={"break_minutes": 0}).json()
assert out["break_minutes"] == 0, out
print(f"  ✓ 무휴게 직원 퇴근 → 휴게 {out['break_minutes']}분")

# ─────────────────────────────────────────────
print("\n[3] 관리자 기록 직접 추가 / 삭제")
r = admin.post("/admin/records/add", data={
    "worker_id": aid, "work_date": "2026-09-07",
    "in_h": 9, "in_m": 0, "out_h": 13, "out_m": 0,
    "break_mode": "manual", "break_minutes": 0, "memo": "오전 근무"})
assert r.status_code == 200, r.text[:300]
assert "물어봄" in r.text
print("  ✓ 첫 번째 기록 추가")

# 같은 날 두 번째 근무도 추가되는지 (키오스크는 막혀 있음)
r = admin.post("/admin/records/add", data={
    "worker_id": aid, "work_date": "2026-09-07",
    "in_h": 18, "in_m": 0, "out_h": 22, "out_m": 0,
    "break_mode": "manual", "break_minutes": 0, "memo": "오후 근무"})
assert r.status_code == 200, r.text[:300]
print("  ✓ 같은 날 두 번째 기록도 추가됨 (특이 케이스 대응)")

with Session(engine) as db:
    recs = db.exec(select(Record).where(Record.worker_id == aid,
                                        Record.work_date == date(2026, 9, 7))).all()
    assert len(recs) == 2, len(recs)
    assert all(r.break_source == "admin" for r in recs)
    target = recs[0].id
print(f"  ✓ 2건 저장 확인, 출처=관리자")

# 잘못된 시각은 거부
r = admin.post("/admin/records/add", data={
    "worker_id": aid, "work_date": "잘못된날짜",
    "in_h": 9, "in_m": 0, "out_h": 18, "out_m": 0})
assert r.status_code == 400, r.status_code
print("  ✓ 잘못된 날짜 거부")

# 삭제
r = admin.post(f"/admin/records/{target}/delete")
assert r.status_code == 200 and r.text.strip() == "", repr(r.text[:100])
with Session(engine) as db:
    assert db.get(Record, target) is None
print("  ✓ 기록 삭제 후 DB 에서 사라짐")

r = admin.post("/admin/records/999999/delete")
assert r.status_code == 404
print("  ✓ 없는 기록 삭제 시 404")

# ─────────────────────────────────────────────
print("\n[4] 근무기록 조회 — '전체 직원'(빈 값)")
r = admin.get("/admin/records?year=2026&month=9&worker_id=")
assert r.status_code == 200, r.text[:300]
print("  ✓ worker_id 빈 값으로 조회해도 200")

r = admin.get(f"/admin/records?year=2026&month=9&worker_id={aid}")
assert r.status_code == 200
print("  ✓ 특정 직원 조회도 정상")

# ─────────────────────────────────────────────
print("\n[5] 시간대")
now_kst = datetime.now(KST)
print(f"  서버 UTC   : {datetime.utcnow().strftime('%H:%M')}")
print(f"  적용 KST   : {now_kst.strftime('%H:%M')}")
with Session(engine) as db:
    rec = db.exec(select(Record).where(Record.worker_id == nid)).first()
    print(f"  출근 기록   : {rec.in_h:02d}:{rec.in_m:02d}  (KST 기준)")
    diff = abs((rec.in_h * 60 + rec.in_m) - (now_kst.hour * 60 + now_kst.minute))
    assert diff <= 40 or diff >= 1400, f"기록 시각이 KST 와 어긋남 (차이 {diff}분)"
print("  ✓ 기록 시각이 한국 시간과 일치")

# ─────────────────────────────────────────────
print("\n[6] 템플릿 렌더링")
html = admin.get("/admin/records?year=2026&month=9").text
for label in ["기록 직접 추가", "삭제", 'id="records-tbody"', "/admin/records/add"]:
    assert label in html, label
print("  ✓ 추가 폼·삭제 버튼 렌더링됨")

kio = pos.get("/").text
for label in ["안 쉼", "식사시간", "brkExtra", "doClockOut", 'break_policy === "none"']:
    assert label in kio, label
assert "10분" not in kio, "10분 단위 조정이 남아 있음"
assert "brkPlus" in kio and "30분 늘리기" in kio
print("  ✓ 휴게 시트: 버튼 3개 + 30분 단위 조정, 10분 단위 제거됨")

print("\n전부 통과 ✅")
