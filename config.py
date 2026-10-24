from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная {name} в .env (см. .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    eljur_domain: str
    eljur_login: str
    eljur_password: str
    eljur_student_id: str | None
    allowed_users: frozenset[int]
    send_time: time
    send_days: frozenset[int]  # 1 = понедельник … 7 = воскресенье
    tz: ZoneInfo
    weeks_ahead: int
    db_path: str


def _weekdays(spec: str) -> frozenset[int]:
    """«1-5» или «1,2,3,4,5» -> номера дней недели (1 = понедельник)."""
    days: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-")
            days.update(range(int(a), int(b) + 1))
        elif part:
            days.add(int(part))
    return frozenset(days) or frozenset(range(1, 8))


def load() -> Config:
    hh, mm = os.getenv("SEND_TIME", "19:00").split(":")
    return Config(
        bot_token=_required("BOT_TOKEN"),
        eljur_domain=os.getenv("ELJUR_DOMAIN", "hselyceum").strip(),
        eljur_login=_required("ELJUR_LOGIN"),
        eljur_password=_required("ELJUR_PASSWORD"),
        eljur_student_id=os.getenv("ELJUR_STUDENT_ID", "").strip() or None,
        allowed_users=frozenset(
            int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x
        ),
        send_time=time(int(hh), int(mm)),
        send_days=_weekdays(os.getenv("SEND_DAYS", "1-7")),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        weeks_ahead=int(os.getenv("WEEKS_AHEAD", "4")),
        db_path=os.getenv("DB_PATH", "eljur_bot.db"),
    )
