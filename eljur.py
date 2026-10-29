"""Клиент и парсер веб-версии «Электронного журнала» (eljur.ru).

Дневник отдаётся HTML-страницами по неделям: /journal-app/u.<id>/week.<N>,
где week.0 — текущая неделя, week.-1 — следующая, week.1 — прошлая.
Домашнее задание в дневнике стоит в тот день, к которому его нужно сдать.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date

import httpx
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
NO_TASK = {"без задания", "нет задания"}


class EljurError(Exception):
    pass


class AuthError(EljurError):
    pass


@dataclass
class Mark:
    key: str
    value: str
    weight: str = ""
    kind: str = ""
    comment: str = ""


@dataclass
class Task:
    id: str
    text: str
    files: list[tuple[str, str]] = field(default_factory=list)  # (имя, ссылка)


@dataclass
class Lesson:
    number: str
    time: str
    subject: str
    topic: str = ""
    test_label: str = ""  # «КР» и т.п., если урок помечен как контрольная
    extra: bool = False  # дополнительные занятия / внеурочка
    marks: list[Mark] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Day:
    date: date
    lessons: list[Lesson] = field(default_factory=list)

    @property
    def main_lessons(self) -> list[Lesson]:
        return [lesson for lesson in self.lessons if not lesson.extra]


# ---------------------------------------------------------------- парсер

def _clean(text: str) -> str:
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _text(el: Tag | None) -> str:
    return _clean(el.get_text()) if el else ""


def _resolve_date(title: str, today: date) -> date | None:
    """«Среда, 21.10» -> date; год берём тот, при котором дата ближе всего к сегодня."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})", title)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            pass
    return min(candidates, key=lambda c: abs((c - today).days)) if candidates else None


def _parse_task(el: Tag, day: date, subject: str) -> Task | None:
    files = [
        (a.get("title") or _text(a), a["href"])
        for a in el.select(".dnevnik-lesson__attach a[href]")
    ]
    complaint = el.select_one('a[href*="task_id="]')
    task_id = None
    if complaint:
        m = re.search(r"task_id=(\d+)", complaint["href"])
        task_id = m.group(1) if m else None

    body = copy.copy(el)
    for junk in body.select('.dnevnik-lesson__attach, a[href*="task_id="], i'):
        junk.decompose()
    for a in body.select("a[href]"):
        # текст ссылок в задании обрезан («https://docs.google.com/spreadshe...»), подставляем полный адрес
        a.replace_with(a["href"])
    text = _text(body)

    if not files and text.lower().strip(" .") in NO_TASK:
        return None
    if not task_id:
        task_id = "h" + hashlib.sha1(f"{day}|{subject}|{text}".encode()).hexdigest()[:16]
    return Task(id=task_id, text=text, files=files)


def _parse_lesson(el: Tag, day: date, extra: bool) -> Lesson:
    subject = _text(el.select_one(".js-rt_licey-dnevnik-subject"))
    lesson = Lesson(
        number=_text(el.select_one(".dnevnik-lesson__number")).rstrip("."),
        time=_text(el.select_one(".dnevnik-lesson__time")),
        subject=subject,
        topic=_text(el.select_one(".js-rt_licey-dnevnik-topic")),
        test_label=_text(el.select_one(".dnevnik-lesson__subject .ej-chip")),
        extra=extra,
    )
    for idx, mark_el in enumerate(el.select(".dnevnik-mark")):
        value_el = mark_el.select_one(".dnevnik-mark__value")
        if not value_el:
            continue
        weight = _text(value_el.select_one(".dnevnik-mark__weight"))
        value = value_el.get("value") or _text(value_el).replace(weight, "").strip()
        info = mark_el.select_one(".item-mark-info")
        if info and info.get("date"):
            key = "|".join(info.get(k, "") for k in ("date", "lesson_id", "num", "nm"))
        else:
            key = f"{day}|{subject}|{lesson.number}|{idx}|{value}"
        lesson.marks.append(Mark(
            key=key,
            value=value,
            weight=weight.lstrip("×x "),
            kind=value_el.get("mtype", ""),
            comment=value_el.get("mcomm", ""),
        ))
    for task_el in el.select(".dnevnik-lesson__task"):
        task = _parse_task(task_el, day, subject)
        if task:
            lesson.tasks.append(task)
    return lesson


def parse_week(html: str, today: date) -> list[Day]:
    soup = BeautifulSoup(html, "html.parser")
    days = []
    for day_el in soup.select(".dnevnik-day"):
        d = _resolve_date(_text(day_el.select_one(".dnevnik-day__title")), today)
        if not d:
            continue
        day = Day(d)
        extra = False
        # внутри дня блоки идут по порядку: уроки, заголовок «доп. занятия», доп. уроки
        for child in day_el.find_all(recursive=False):
            classes = child.get("class", [])
            if "dnevnik-day__header--sub" in classes:
                extra = True
            elif "dnevnik-day__lessons" in classes:
                day.lessons += [_parse_lesson(l, d, extra) for l in child.select(".dnevnik-lesson")]
        days.append(day)
    return days


@dataclass
class Grade:
    """Оценка со страницы «Успеваемость»."""
    key: str
    subject: str
    date: date
    value: str  # «5», «4/4» (двойная), «Н» (пропуск)
    weight: float | None = None
    kind: str = ""  # «к» констатирующая, «т» творческая, «ф» формирующая; пусто — тип не задан

    @property
    def numbers(self) -> list[int]:
        return [int(p) for p in self.value.split("/") if p.isdigit()]


@dataclass
class SubjectGrades:
    subject: str
    grades: list[Grade] = field(default_factory=list)

    @property
    def average(self) -> float | None:
        """Средневзвешенная; оценка без указанного веса считается с весом 1."""
        total = weights = 0.0
        for g in self.grades:
            w = g.weight if g.weight is not None else 1.0
            for n in g.numbers:
                total += n * w
                weights += w
        return total / weights if weights else None


_SET_RE = re.compile(r'"(\d+)":(\{"id":"\1"[^{}]*\})')


def _mark_sets(html: str) -> dict[str, str]:
    """set_id -> короткий тип оценки. Наборы лежат в JSON внутри скрипта страницы:
    "791258":{"id":"791258","name":"Формирующая",...,"mtype_short":"Ф"}."""
    sets = {}
    for m in _SET_RE.finditer(html):
        try:
            short = json.loads(m.group(2)).get("mtype_short") or ""
        except ValueError:
            continue
        sets[m.group(1)] = short.lower()
    return sets


def parse_grades(html: str) -> list[SubjectGrades]:
    """Таблица успеваемости: колонки .cells_marks, в каждой по ячейке на предмет."""
    soup = BeautifulSoup(html, "html.parser")
    sets = _mark_sets(html)
    subjects = {
        cell["name"]: SubjectGrades(cell["name"])
        for cell in soup.select("#g0_fio .cell[name]")
    }
    for cell in soup.select(".cells_marks .cell[mark_date]"):
        mark_date, name = cell.get("mark_date", ""), cell.get("name", "")
        value = _text(cell.select_one(".cell-data"))
        if not mark_date or not name or not value:
            continue
        try:
            weight = float(cell["weight"]) if cell.get("weight") else None
        except ValueError:
            weight = None
        subjects.setdefault(name, SubjectGrades(name)).grades.append(Grade(
            key=f"{name}|{cell.get('id', '')}|{cell.get('set_id', '')}|{mark_date}",
            subject=name,
            date=date.fromisoformat(mark_date),
            value=value,
            weight=weight,
            kind=sets.get(cell.get("set_id", ""), ""),
        ))
    for s in subjects.values():
        s.grades.sort(key=lambda g: g.date)
    return list(subjects.values())


# ---------------------------------------------------------------- клиент

class EljurClient:
    def __init__(self, domain: str, login: str, password: str, student_id: str | None = None):
        self.base = f"https://{domain}.eljur.ru"
        self._login = login
        self._password = password
        self.student_id = student_id
        self._logged_in = False
        self._http = httpx.AsyncClient(
            base_url=self.base,
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": USER_AGENT},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def login(self) -> None:
        self._http.cookies.clear()
        url = "/ajaxauthorize"
        # форма входа шлёт multipart; сервер отвечает {"errors": [...]} или {"actions": [...]}
        for _ in range(3):
            r = await self._http.post(
                url,
                files={
                    "username": (None, self._login),
                    "password": (None, self._password),
                    "return_uri": (None, "/journal-app"),
                },
                headers={"Referer": f"{self.base}/authorize?return_uri=%2Fjournal-app"},
            )
            try:
                data = r.json()
            except ValueError:
                raise EljurError(f"Неожиданный ответ при входе (HTTP {r.status_code})")
            if data.get("errors"):
                texts = [e.get("text", str(e)) if isinstance(e, dict) else str(e) for e in data["errors"]]
                raise AuthError("Журнал не пустил: " + "; ".join(texts))
            actions = data.get("actions") or []
            next_login = next((a for a in actions if a.get("type") == "login" and a.get("url")), None)
            if next_login:
                url = next_login["url"]
                continue
            redirect = next((a for a in actions if a.get("type") == "redirect" and a.get("url")), None)
            if redirect:
                await self._http.get(redirect["url"])
            self._logged_in = True
            return
        raise AuthError("Слишком много шагов входа")

    @staticmethod
    def _is_login_page(r: httpx.Response) -> bool:
        return r.url.path.startswith("/authorize") or "standalone.login" in r.text

    async def _get(self, path: str) -> str:
        if not self._logged_in:
            await self.login()
        r = await self._http.get(path)
        if self._is_login_page(r):  # сессия протухла
            await self.login()
            r = await self._http.get(path)
            if self._is_login_page(r):
                raise AuthError("Не удалось войти в журнал")
        r.raise_for_status()
        return r.text

    async def _ensure_student(self) -> str:
        if not self.student_id:
            html = await self._get("/journal-app")
            m = re.search(r"/journal-app/u\.(\d+)", html)
            if not m:
                raise EljurError("Не удалось определить id ученика, укажите ELJUR_STUDENT_ID в .env")
            self.student_id = m.group(1)
        return self.student_id

    async def get_week(self, offset: int, today: date) -> list[Day]:
        student = await self._ensure_student()
        return parse_week(await self._get(f"/journal-app/u.{student}/week.{offset}"), today)

    async def get_grades(self) -> list[SubjectGrades]:
        student = await self._ensure_student()
        return parse_grades(await self._get(f"/journal-student-grades-action/u.{student}"))

    async def get_days(self, today: date, weeks_back: int = 1, weeks_ahead: int = 4) -> dict[date, Day]:
        days: dict[date, Day] = {}
        for offset in range(weeks_back, -weeks_ahead - 1, -1):
            for day in await self.get_week(offset, today):
                days[day.date] = day
        return days
