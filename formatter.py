"""Тексты сообщений для Telegram (parse_mode=HTML)."""
from __future__ import annotations

import re
from datetime import date
from html import escape

from eljur import Day, Lesson, SubjectGrades
from storage import StoredMark, StoredTask

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# слова, по которым задание считается «серьёзным» и требующим подготовки заранее
IMPORTANT_RE = re.compile(
    r"контрольн|\bкр\b|проверочн|самостоятельн|\bср\b|тест|зач[её]т|проект|презентац|творческ"
    r"|сочинени|доклад|выступлени|экзамен|огэ|егэ|констатирующ|лабораторн|выучить|наизусть",
    re.IGNORECASE,
)
URGENT_DAYS = 3


def fmt_date(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m}"


def plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = "день"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


def _relative(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == 2:
        return "послезавтра"
    return f"через {plural_days(delta)}"


def _files(files: list[tuple[str, str]]) -> str:
    return ", ".join(f'<a href="{escape(url)}">📎 {escape(name)}</a>' for name, url in files)


def _task_body(text: str, files: list[tuple[str, str]]) -> str:
    body = escape(text).replace("\n", " ")
    if files:
        body += " " + _files(files)
    return body


def _group(lessons: list[Lesson]) -> list[tuple[str, list[Lesson]]]:
    """Сдвоенные уроки одного предмета подряд объединяем: «1–2. История»."""
    groups: list[list[Lesson]] = []
    for lesson in lessons:
        prev = groups[-1][-1] if groups else None
        if prev and prev.subject == lesson.subject and prev.extra == lesson.extra:
            groups[-1].append(lesson)
        else:
            groups.append([lesson])
    result = []
    for group in groups:
        first, last = group[0], group[-1]
        if first.number and last.number and first.number != last.number:
            num = f"{first.number}–{last.number}. "
        elif first.number:
            num = f"{first.number}. "
        else:
            num = "• "
        result.append((num, group))
    return result


def _mark_line(subject: str, value: str, weight: str, kind: str, comment: str) -> str:
    details = ", ".join(x for x in (kind, f"вес {weight}" if weight else "", comment) if x)
    line = f"<b>{escape(value)}</b> — {escape(subject)}"
    return line + (f" <i>({escape(details)})</i>" if details else "")


# ---------------------------------------------------------------- секции

def section_today(day: Day | None, today: date) -> str:
    title = f"📖 <b>Сегодня, {WEEKDAYS[today.weekday()]} {today:%d.%m}</b>"
    if not day or not day.lessons:
        return f"{title}\nУроков не было."
    lines = [title]
    for num, group in _group(day.lessons):
        first = group[0]
        topics = list(dict.fromkeys(l.topic for l in group if l.topic))
        line = f"{num}{escape(first.subject)}"
        if any(l.test_label for l in group):
            line += f" 📝 <b>{escape(next(l.test_label for l in group if l.test_label))}</b>"
        if topics:
            line += f"\n    <i>{escape('; '.join(topics))}</i>"
        lines.append(line)
    marks = [(l.subject, m) for l in day.lessons for m in l.marks]
    if marks:
        lines.append("\n⭐ <b>Оценки за сегодня:</b>")
        lines += [_mark_line(s, m.value, m.weight, m.kind, m.comment) for s, m in marks]
    return "\n".join(lines)


def section_new_marks(marks: list[StoredMark], today: date) -> str:
    marks = [m for m in marks if m.day != today]  # сегодняшние уже показаны в «Сегодня»
    if not marks:
        return ""
    lines = ["⭐ <b>Новые оценки за прошлые дни:</b>"]
    lines += [f"{fmt_date(m.day)}: " + _mark_line(m.subject, m.value, m.weight, m.kind, m.comment) for m in marks]
    return "\n".join(lines)


def section_next_day(day: Day | None, today: date) -> str:
    if not day:
        return "📚 <b>Ближайших уроков в журнале нет</b> (каникулы?)"
    weekday = f"{WEEKDAYS[day.date.weekday()]} {day.date:%d.%m}"
    if (day.date - today).days == 1:
        title = f"📚 <b>Расписание и ДЗ на завтра, {weekday}</b>"
    else:
        title = (f"🗓 Завтра уроков нет.\n"
                 f"📚 <b>Расписание и ДЗ на {weekday}</b> ({_relative(day.date, today)})")
    lines = [title]
    for num, group in _group(day.lessons):
        first = group[0]
        time = first.time.split("–")[0] if first.time else ""
        line = f"\n{num}{escape(first.subject)}"
        if time:
            line = f"\n{num}<code>{time}</code> {escape(first.subject)}"
        if first.extra:
            line += " <i>(доп.)</i>"
        label = next((l.test_label for l in group if l.test_label), "")
        if label:
            line += f" 📝 <b>{escape(label)}</b>"
        lines.append(line)
        tasks = [t for l in group for t in l.tasks]
        for task in tasks:
            lines.append(f"    ✏️ {_task_body(task.text, task.files)}")
        if not tasks and not first.extra:
            lines.append("    <i>без задания</i>")
    return "\n".join(lines)


def _is_important(task: StoredTask) -> bool:
    return bool(task.test_label) or bool(IMPORTANT_RE.search(task.text))


def section_long(tasks: list[StoredTask], today: date, title: str = "⏳ <b>Задания на будущее</b>") -> str:
    tasks = [t for t in tasks if not t.done]
    if not tasks:
        return f"{title}\nДолгосрочных заданий нет 🎉"
    lines = [title, "<i>Делай заранее, они будут приходить каждый день до сдачи.</i>"]
    current = None
    for task in tasks:
        if task.due != current:
            current = task.due
            left = (task.due - today).days
            fire = "🔥 " if left <= URGENT_DAYS else ""
            lines.append(f"\n{fire}<b>к {fmt_date(task.due)}</b> · {_relative(task.due, today)}")
        mark = "❗️" if _is_important(task) else "▫️"
        test = f" 📝 <b>{escape(task.test_label)}</b>" if task.test_label else ""
        lines.append(f"{mark} <b>{escape(task.subject)}</b>{test}: {_task_body(task.text, task.files)}")
    return "\n".join(lines)


def section_new_tasks(tasks: list[StoredTask], today: date) -> str:
    if not tasks:
        return ""
    lines = ["🆕 <b>Новые задания в журнале:</b>"]
    lines += [
        f"• {escape(t.subject)} (к {fmt_date(t.due)}): {_task_body(t.text, [])}"
        for t in tasks
    ]
    return "\n".join(lines)


def _avg_icon(avg: float) -> str:
    return "🟢" if avg >= 4.5 else "🟡" if avg >= 3.5 else "🔴"


def section_grades(subjects: list[SubjectGrades], new_keys: set[str]) -> str:
    with_marks = [s for s in subjects if s.grades]
    new_count = sum(g.key in new_keys for s in with_marks for g in s.grades)
    lines = ["📊 <b>Оценки по предметам</b>"]
    lines.append(f"🆕 Новых: <b>{new_count}</b> (выделены жирным)" if new_count else "Новых оценок нет.")
    for s in with_marks:
        avg = s.average
        head = f"\n{_avg_icon(avg)} {escape(s.subject)} · ср. {avg:.2f}" if avg is not None else f"\n⚪️ {escape(s.subject)}"
        marks = []
        for g in s.grades:
            value = escape(g.value.lower() if not g.numbers else g.value)
            marks.append(f"<b>{value}</b>" if g.key in new_keys else value)
        lines += [head, "    " + "  ".join(marks)]
    empty = [s.subject for s in subjects if not s.grades]
    if empty:
        lines.append(f"\n<i>Пока без оценок: {escape(', '.join(empty))}</i>")
    lines.append("\n<i>ср. = средневзвешенная с учётом веса оценок; н = пропуск</i>")
    return "\n".join(lines)


def daily_report(
    today: date,
    today_day: Day | None,
    next_day: Day | None,
    long_tasks: list[StoredTask],
    new_tasks: list[StoredTask],
    new_marks: list[StoredMark],
) -> str:
    parts = [
        section_today(today_day, today),
        section_new_marks(new_marks, today),
        section_new_tasks(new_tasks, today),
        section_next_day(next_day, today),
        section_long(long_tasks, today),
    ]
    return "\n\n".join(p for p in parts if p)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Telegram ограничивает сообщение 4096 символами; режем по строкам."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # очень длинная строка без переносов
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
