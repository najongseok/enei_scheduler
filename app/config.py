"""환경설정. 운영 시엔 .env 로 주입하세요 (비밀키를 코드에 넣지 마세요)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # 세션 서명 키 — 운영 배포 전 반드시 교체
    secret_key: str = field(
        default_factory=lambda: os.getenv("SECRET_KEY", "change-me-in-production"))
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./attendance.db"))

    # Nginx/Cloudflare 등 신뢰하는 프록시 뒤에 있을 때만 True
    trust_proxy: bool = field(
        default_factory=lambda: os.getenv("TRUST_PROXY", "0") == "1")

    # HTTPS 로 서비스할 때 True (Geolocation API 는 HTTPS 필수)
    secure_cookies: bool = field(
        default_factory=lambda: os.getenv("SECURE_COOKIES", "0") == "1")

    timezone: str = "Asia/Seoul"

    # 출근/퇴근 시각 스냅 방식 — payroll.snap_minutes 참고
    snap_in_mode: str = field(default_factory=lambda: os.getenv("SNAP_IN", "near"))
    snap_out_mode: str = field(default_factory=lambda: os.getenv("SNAP_OUT", "late"))


settings = Settings()
