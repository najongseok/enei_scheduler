# 이번 업데이트 적용 안내

## ⚠️ 먼저 — Supabase 에서 SQL 한 줄 실행 (필수)

기존 DB 에는 "직원 1명당 하루 1건"이라는 제약이 걸려 있습니다.
이게 남아 있으면 **같은 날 두 번째 근무 기록 추가가 실패**합니다.

Supabase → 왼쪽 메뉴 **SQL Editor** → 아래 붙여넣고 **Run**:

```sql
ALTER TABLE record DROP CONSTRAINT IF EXISTS record_worker_id_work_date_key;
```

한 번만 실행하면 됩니다. 기존 기록은 그대로 유지됩니다.

---

## 바뀐 파일

아래 파일들을 프로젝트에 덮어쓴 뒤 push 하세요.

| 파일 | 바뀐 내용 |
|---|---|
| `app/payroll.py` | 가산수당 기준 8시간 → **9시간** |
| `app/models.py` | 하루 1건 UNIQUE 제약 제거 |
| `app/main.py` | 프로세스 시작 시 시간대를 한국으로 고정 |
| `app/security.py` | 세션 시각 한국 시간 적용 |
| `app/routers/kiosk.py` | 모든 시각 계산 한국 시간 적용 |
| `app/routers/admin.py` | 기록 추가·삭제 라우트, 조회 버그 수정, 시각 한국 시간 |
| `app/templates/kiosk.html` | 휴게 시트 개편, 퇴근 버그 수정, 로딩 표시 |
| `app/templates/admin_records.html` | 기록 추가 폼, 삭제 열 |
| `app/templates/_record_row.html` | 두 자리 시각 표시, 삭제 버튼 |

```bash
git add .
git commit -m "가산수당 9시간 기준, 휴게시트 개편, 기록 추가/삭제, 시간대 수정"
git push
```

---

## 검증 방법

```bash
python fix_test.py     # 이번 수정분
python smoke_test.py   # 전체 기능 회귀 검사
```
