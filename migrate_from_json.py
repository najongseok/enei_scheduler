"""
기존 data.json → 웹앱 DB 이관.

    python migrate_from_json.py /경로/data.json

기존 기록의 휴게시간은 '자동 차감된 값'이므로 break_override 를 그대로
비워둡니다(= 자동). 실제로는 못 쉰 날이 섞여 있을 수 있으니, 이관 후
관리자 화면에서 문제되는 날짜만 수동으로 고치면 됩니다.
"""
from __future__ import annotations

import json
import sys
from datetime import date

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Record, Worker
from app.payroll import legal_break
from app.security import hash_secret


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    init_db()
    with Session(engine) as db:
        name_to_id: dict[str, int] = {}

        for name, info in data.get("workers", {}).items():
            existing = db.exec(select(Worker).where(Worker.name == name)).first()
            if existing:
                name_to_id[name] = existing.id
                continue
            w = Worker(
                name=name,
                pin_hash=hash_secret(str(info.get("pin", "0000"))),
                hourly=int(info.get("hourly", 0)),
                contract_days=int(info.get("contract_days", 5)),
                employment_type=info.get("employment_type", "파트타임"),
                extra_eligible=bool(info.get("extra_eligible", False)),
                break_policy="ask",            # 이관 후에는 근로자에게 묻는 방식이 기본
                weekly_holiday_policy="auto",
            )
            db.add(w)
            db.commit()
            db.refresh(w)
            name_to_id[name] = w.id
            print(f"직원 등록: {name}")

        added = skipped = 0
        for r in data.get("records", []):
            wid = name_to_id.get(r["name"])
            if wid is None:
                skipped += 1
                continue
            d = date(r["year"], r["month"], r["day"])
            if db.exec(select(Record).where(Record.worker_id == wid,
                                            Record.work_date == d)).first():
                skipped += 1
                continue

            total = r.get("minutes")
            brk = r.get("break_minutes")
            if total is not None and brk is not None:
                total = total + brk

            db.add(Record(
                worker_id=wid, work_date=d,
                in_h=r.get("in_h"), in_m=r.get("in_m"),
                out_h=r.get("out_h"), out_m=r.get("out_m"),
                total_minutes=total,
                break_minutes=brk,
                break_override=None,           # 자동 계산 상태 유지
                break_source="auto",
                minutes=r.get("minutes"),
                is_holiday=bool(r.get("is_holiday", False)),
                memo="data.json 이관",
            ))
            added += 1
        db.commit()

    print(f"\n기록 {added}건 이관, {skipped}건 건너뜀(중복 또는 미등록 직원).")
    print("PIN 은 해시로 저장돼 원문을 볼 수 없습니다. 필요하면 관리자 화면에서 재발급하세요.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python migrate_from_json.py <data.json 경로>")
    else:
        main(sys.argv[1])
