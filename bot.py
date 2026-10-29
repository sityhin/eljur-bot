"""Telegram-бот: каждый день присылает сводку из «Электронного журнала»."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)  # .env и база лежат рядом с bot.py, откуда бы его ни запустили

import config  # noqa: E402
import formatter as fmt  # noqa: E402
from eljur import Day, EljurClient, EljurError, SubjectGrades
from storage import Storage, StoredTask

log = logging.getLogger("eljur_bot")
cfg = config.load()
storage = Storage(cfg.db_path)
client = EljurClient(cfg.eljur_domain, cfg.eljur_login, cfg.eljur_password, cfg.eljur_student_id)
sync_lock = asyncio.Lock()
router = Router()

def send_time() -> time:
    """Время сводки: владелец меняет его командой /time, иначе берём SEND_TIME из настроек."""
    value = storage.get_meta("send_time")
    if value:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    return cfg.send_time


def help_text() -> str:
    return (
        "Я слежу за электронным журналом и каждый день в "
        f"{send_time():%H:%M} присылаю сводку:\n"
        "• что было сегодня (уроки, темы) и новые оценки;\n"
        "• расписание и ДЗ на завтра;\n"
        "• задания на будущее, которые надо делать заранее. Они приходят каждый день до даты сдачи.\n\n"
        "/report: сводка прямо сейчас\n"
        "/tomorrow: расписание и ДЗ на завтра\n"
        "/long: задания на будущее + кнопки «сделано»\n"
        "/marks: оценки по всем предметам с датой и типом, новые за день отмечены ‼️\n"
        "/pause: приостановить рассылку на время (например, на каникулы)\n"
        "/resume: возобновить рассылку раньше срока\n"
        "/time: время сводки (менять может только владелец)\n"
        "/stop: отписаться от ежедневной рассылки"
    )


@dataclass
class Snapshot:
    today: date
    days: dict[date, Day]
    grades: list[SubjectGrades] | None = None  # None — страницу успеваемости не загружали (или не смогли)

    @property
    def today_day(self) -> Day | None:
        return self.days.get(self.today)

    @property
    def next_day(self) -> Day | None:
        """Ближайший учебный день после сегодняшнего (в выходные и каникулы может быть не завтра)."""
        future = [d for d in sorted(self.days) if d > self.today and self.days[d].main_lessons]
        return self.days[future[0]] if future else None

    @property
    def long_after(self) -> date:
        """Долгосрочные = всё, что сдаётся позже ближайшего учебного дня."""
        nd = self.next_day
        return nd.date if nd else self.today


def now() -> datetime:
    return datetime.now(cfg.tz)


async def refresh(with_grades: bool = False) -> Snapshot:
    async with sync_lock:
        today = now().date()
        days = await client.get_days(today, weeks_back=1, weeks_ahead=cfg.weeks_ahead)
        storage.sync(days, today)
        grades = None
        if with_grades:
            try:
                grades = await client.get_grades()
                storage.sync_grades(grades, today)
            except Exception:  # сводка уйдёт и без них: оценки тогда возьмём из дневника
                log.exception("grades failed")
        return Snapshot(today, days, grades)


def build_daily(snap: Snapshot) -> str:
    since = storage.get_meta("last_report", "")
    new_grades = None
    if snap.grades is not None:
        # со страницы успеваемости видны и оценки, выставленные сегодня за старые уроки
        keys = storage.new_grade_keys(since)
        new_grades = [g for s in snap.grades for g in s.grades if g.key in keys]
    return fmt.daily_report(
        today=snap.today,
        today_day=snap.today_day,
        next_day=snap.next_day,
        long_tasks=storage.upcoming(snap.long_after, include_done=False),
        new_tasks=storage.new_tasks_since(since, snap.today),
        new_marks=storage.new_marks_since(since),
        new_grades=new_grades,
    )


def task_button(task: StoredTask) -> InlineKeyboardButton:
    icon = "↩️" if task.done else "✅"
    label = f"{icon} {fmt.fmt_date(task.due)} {task.subject}: {task.text}".replace("\n", " ")
    return InlineKeyboardButton(text=label[:60], callback_data=f"t:{task.id}")


def long_keyboard(snap: Snapshot) -> InlineKeyboardMarkup | None:
    rows = [[task_button(task)] for task in storage.upcoming(snap.long_after)]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def long_text(snap: Snapshot) -> str:
    tasks = storage.upcoming(snap.long_after)
    text = fmt.section_long(tasks, snap.today)
    done = [t for t in tasks if t.done]
    if done:
        text += f"\n\n☑️ <i>Отмечено сделанными: {len(done)} (вернуть можно кнопкой ↩️)</i>"
    return text + "\n\nНажми на задание ниже, чтобы отметить его сделанным."


async def send_long(bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    chunks = fmt.split_message(text)
    for i, chunk in enumerate(chunks):
        markup = keyboard if i == len(chunks) - 1 else None
        await bot.send_message(chat_id, chunk, reply_markup=markup)


async def with_data(message: Message, action, with_grades: bool = False) -> None:
    wait = await message.answer("⏳ Захожу в журнал…")
    try:
        snap = await refresh(with_grades)
    except EljurError as e:
        await wait.edit_text(f"⚠️ {e}")
        return
    except Exception as e:  # сеть, таймауты, изменившаяся вёрстка
        log.exception("refresh failed")
        await wait.edit_text(f"⚠️ Не удалось получить данные из журнала: {e}")
        return
    await wait.delete()
    await action(snap)


# ---------------------------------------------------------------- доступ

# Владельцы — ALLOWED_USERS из настроек. Остальных (членов семьи) владелец пускает кнопкой,
# они хранятся в meta как user:<id> = имя.

def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id in cfg.allowed_users


def allowed(user_id: int | None) -> bool:
    return is_owner(user_id) or bool(user_id and storage.get_meta(f"user:{user_id}"))


async def from_allowed(event: Message | CallbackQuery) -> bool:
    # асинхронный фильтр выполняется в основном потоке; синхронный aiogram унёс бы в другой, а SQLite так нельзя
    return allowed(event.from_user.id if event.from_user else None)


router.message.filter(from_allowed)
router.callback_query.filter(from_allowed)
guest_router = Router()


@guest_router.message()
async def no_access(message: Message) -> None:
    user = message.from_user
    request = storage.get_meta(f"request:{user.id}")
    text = f"Это личный бот, у тебя пока нет доступа.\nТвой Telegram ID: <code>{user.id}</code>."
    if request == "denied":
        await message.answer(text)
        return
    if request:
        await message.answer(text + "\nЗапрос владельцу уже отправлен, жди ответа.")
        return
    storage.set_meta(f"request:{user.id}", "sent")
    name = escape(user.full_name or str(user.id))
    username = f" (@{escape(user.username)})" if user.username else ""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пустить", callback_data=f"a:{user.id}"),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"d:{user.id}"),
    ]])
    for owner in cfg.allowed_users:
        try:
            await message.bot.send_message(
                owner, f"🙋 {name}{username} просит доступ к боту.", reply_markup=keyboard
            )
        except Exception:
            log.exception("access request to %s failed", owner)
    await message.answer(text + "\nЯ отправил владельцу запрос: как только он нажмёт «Пустить», я напишу.")


@router.callback_query(F.data.regexp(r"^[ad]:\d+$"))
async def on_access(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Пускать в бота может только владелец")
        return
    user_id = int(callback.data[2:])
    old_text = callback.message.html_text if callback.message else ""
    name = old_text.removeprefix("🙋 ").removesuffix(" просит доступ к боту.") or str(user_id)
    if callback.data[0] == "a":
        storage.set_meta(f"user:{user_id}", name)
        storage.set_meta(f"request:{user_id}", "")
        storage.add_subscriber(user_id)
        await callback.answer("Доступ открыт")
        await callback.message.edit_text(f"✅ {name} теперь пользуется ботом. Список: /users")
        await callback.bot.send_message(
            user_id, "✅ Доступ открыт! Ты подписан на ежедневную сводку.\n\n" + help_text(), reply_markup=MENU
        )
    else:
        storage.set_meta(f"request:{user_id}", "denied")
        await callback.answer("Отказано")
        await callback.message.edit_text(f"❌ {name}: в доступе отказано.")


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not is_owner(message.from_user.id):
        await message.answer("Список пользователей видит только владелец.")
        return
    members = {k.removeprefix("user:"): v for k, v in storage.meta_with_prefix("user:").items() if v}
    if not members:
        await message.answer("Кроме владельцев, ботом никто не пользуется.\n"
                             "Чтобы добавить человека, пусть он напишет боту: тебе придёт кнопка «Пустить».")
        return
    rows = [[InlineKeyboardButton(text=f"❌ Убрать {name}"[:60], callback_data=f"r:{uid}")]
            for uid, name in members.items()]
    await message.answer(
        "Пользуются ботом:\n" + "\n".join(f"• {name}" for name in members.values())
        + "\n\nНовый человек просто пишет боту, тебе придёт кнопка «Пустить».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.regexp(r"^r:\d+$"))
async def on_remove_user(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Убирать может только владелец")
        return
    user_id = int(callback.data[2:])
    name = storage.get_meta(f"user:{user_id}") or str(user_id)
    storage.set_meta(f"user:{user_id}", "")
    storage.remove_subscriber(user_id)
    await callback.answer("Доступ закрыт")
    await callback.message.edit_text(f"Доступ для {name} закрыт. Список: /users")


# ---------------------------------------------------------------- команды

BTN_REPORT, BTN_TOMORROW, BTN_LONG, BTN_MARKS = "📋 Сводка", "📚 Завтра", "⏳ На будущее", "📊 Оценки"
BTN_PAUSE = "⏸ Пауза рассылки"
MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_LONG), KeyboardButton(text=BTN_MARKS)],
        [KeyboardButton(text=BTN_PAUSE)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    storage.add_subscriber(message.chat.id)
    await message.answer("Привет! Ты подписан на ежедневную сводку.\n\n" + help_text(), reply_markup=MENU)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(help_text(), reply_markup=MENU)


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    storage.remove_subscriber(message.chat.id)
    await message.answer("Рассылка выключена. /start, чтобы включить снова.")


# ---------------------------------------------------------------- пауза рассылки

PAUSE_OPTIONS = [(7, "1 неделя"), (14, "2 недели"), (30, "Месяц")]


def paused_until(chat_id: int, today: date) -> date | None:
    """До какого дня (включительно) рассылка в этом чате на паузе; None, если паузы нет или она кончилась."""
    value = storage.get_meta(f"pause:{chat_id}")
    until = date.fromisoformat(value) if value else None
    return until if until and until >= today else None


def set_pause(chat_id: int, until: date | None) -> None:
    storage.set_meta(f"pause:{chat_id}", until.isoformat() if until else "")


def parse_until(text: str, today: date) -> date | None:
    """«10.11» или «10.11.2026». Дата без года, которая уже прошла, относится к следующему году."""
    parts = text.strip().split(".")
    try:
        if len(parts) == 2:
            d = date(today.year, int(parts[1]), int(parts[0]))
            return d if d >= today else date(today.year + 1, d.month, d.day)
        if len(parts) == 3:
            d = date(int(parts[2]), int(parts[1]), int(parts[0]))
            return d if d >= today else None
    except ValueError:
        pass
    return None


def pause_text(until: date) -> str:
    return (
        f"⏸ Ежедневная сводка на паузе до {fmt.fmt_date(until)} включительно, "
        f"снова начнёт приходить с {fmt.fmt_date(until + timedelta(days=1))}.\n"
        "Кнопки и команды работают как обычно. /resume, чтобы включить рассылку раньше."
    )


@router.message(F.text == BTN_PAUSE)
@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject | None = None) -> None:
    today = now().date()
    if command and command.args:
        until = parse_until(command.args, today)
        if until is None:
            await message.answer("Не понял дату. Напиши, например, <code>/pause 10.11</code>: пауза до 10 ноября включительно.")
            return
        set_pause(message.chat.id, until)
        await message.answer(pause_text(until))
        return
    current = paused_until(message.chat.id, today)
    text = f"Сейчас рассылка на паузе до {fmt.fmt_date(current)}.\n\n" if current else ""
    text += (
        "На сколько приостановить ежедневную сводку?\n"
        "Или укажи последний день паузы: <code>/pause 10.11</code>."
    )
    rows = [[InlineKeyboardButton(text=label, callback_data=f"p:{days}") for days, label in PAUSE_OPTIONS]]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("p:"))
async def on_pause(callback: CallbackQuery) -> None:
    until = now().date() + timedelta(days=int(callback.data[2:]) - 1)
    set_pause(callback.message.chat.id, until)
    await callback.answer("Пауза включена")
    await callback.message.edit_text(pause_text(until))


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    set_pause(message.chat.id, None)
    await message.answer(f"▶️ Рассылка снова включена: сводка придёт в {send_time():%H:%M}.")


# ---------------------------------------------------------------- время сводки

def parse_time(text: str) -> time | None:
    """«19:00», «19.00», «9:30» или просто «19»."""
    parts = text.strip().replace(".", ":").split(":")
    try:
        if len(parts) == 1:
            return time(int(parts[0]), 0)
        if len(parts) == 2:
            return time(int(parts[0]), int(parts[1]))
    except ValueError:
        pass
    return None


@router.message(Command("time"))
async def cmd_time(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        await message.answer(f"Сводка приходит в {send_time():%H:%M}. Поменять время может только владелец.")
        return
    if not command.args:
        await message.answer(
            f"🕒 Сводка приходит в <b>{send_time():%H:%M}</b>.\n"
            "Чтобы поменять, напиши, например, <code>/time 19:00</code>."
        )
        return
    new = parse_time(command.args)
    if new is None:
        await message.answer("Не понял время. Напиши, например, <code>/time 19:00</code>.")
        return
    storage.set_meta("send_time", f"{new:%H:%M}")
    text = f"🕒 Готово: сводка будет приходить в <b>{new:%H:%M}</b>."
    if storage.get_meta("last_daily") == now().date().isoformat():
        text += "\nСегодняшняя уже была, по новому времени — со следующего раза."
    await message.answer(text)


@router.message(F.text == BTN_REPORT)
@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    async def act(snap: Snapshot) -> None:
        await send_long(message.bot, message.chat.id, build_daily(snap))
    await with_data(message, act, with_grades=True)


@router.message(F.text == BTN_TOMORROW)
@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message) -> None:
    async def act(snap: Snapshot) -> None:
        await send_long(message.bot, message.chat.id, fmt.section_next_day(snap.next_day, snap.today))
    await with_data(message, act)


@router.message(F.text == BTN_LONG)
@router.message(Command("long"))
async def cmd_long(message: Message) -> None:
    async def act(snap: Snapshot) -> None:
        await send_long(message.bot, message.chat.id, long_text(snap), long_keyboard(snap))
    await with_data(message, act)


@router.message(F.text == BTN_MARKS)
@router.message(Command("marks"))
async def cmd_marks(message: Message) -> None:
    wait = await message.answer("⏳ Захожу в журнал…")
    try:
        async with sync_lock:
            subjects = await client.get_grades()
            storage.sync_grades(subjects, now().date())
    except Exception as e:
        log.exception("grades failed")
        await wait.edit_text(f"⚠️ Не удалось получить оценки из журнала: {e}")
        return
    await wait.delete()
    new_keys = storage.new_grade_keys(grades_since(message.chat.id))
    await send_long(message.bot, message.chat.id, fmt.section_grades(subjects, new_keys))


def grades_since(chat_id: int) -> str:
    """С какого момента оценки в «Оценках» считаются новыми: с последнего просмотра *до сегодняшнего дня*.
    Поэтому новая оценка остаётся выделенной весь день, сколько бы раз ни открывали «Оценки».
    Время — как у first_seen в базе (datetime.now() сервера)."""
    stamp = datetime.now().isoformat(timespec="seconds")
    last = storage.get_meta(f"grades_view:{chat_id}")
    if last and last[:10] < stamp[:10]:
        storage.set_meta(f"grades_seen:{chat_id}", last)
    storage.set_meta(f"grades_view:{chat_id}", stamp)
    return storage.get_meta(f"grades_seen:{chat_id}", "")


@router.callback_query(F.data.startswith("t:"))
async def on_toggle(callback: CallbackQuery) -> None:
    task_id = callback.data[2:]
    task = storage.get_task(task_id)
    if not task:
        await callback.answer("Задание не найдено")
        return
    done = storage.toggle_done(task_id)
    await callback.answer("Отмечено сделанным ✅" if done else "Вернул в список")
    # перерисовываем те же кнопки по сохранённым данным, без захода в журнал
    markup = callback.message.reply_markup if callback.message else None
    if not markup:
        return
    rows = []
    for row in markup.inline_keyboard:
        t = storage.get_task(row[0].callback_data[2:])
        if t:
            rows.append([task_button(t)])
    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:  # например, «message is not modified»
        pass


# ---------------------------------------------------------------- рассылка

async def send_daily(bot: Bot) -> None:
    snap = await refresh(with_grades=True)
    text = build_daily(snap)
    for chat_id in storage.subscribers():
        if paused_until(chat_id, snap.today):
            continue
        try:
            await send_long(bot, chat_id, text)
        except Exception:
            log.exception("send to %s failed", chat_id)
    storage.set_meta("last_report", datetime.now().isoformat(timespec="seconds"))


def daily_due(current: datetime) -> bool:
    """Сегодня день рассылки, время уже наступило, а сводка сегодня ещё не уходила."""
    return (
        current.isoweekday() in cfg.send_days
        and current.time() >= send_time()
        and storage.get_meta("last_daily") != current.date().isoformat()
    )


async def try_daily(bot: Bot, current: datetime) -> bool:
    """Отправляет сводку. При ошибке предупреждает подписчиков; повтор — забота вызывающего."""
    try:
        await send_daily(bot)
        storage.set_meta("last_daily", current.date().isoformat())
        return True
    except Exception as e:
        log.exception("daily report failed")
        for chat_id in storage.subscribers():
            if paused_until(chat_id, current.date()):
                continue
            try:
                await bot.send_message(chat_id, f"⚠️ Не получилось собрать сводку, попробую через 10 минут: {e}")
            except Exception:
                pass
        return False


async def daily_loop(bot: Bot) -> None:
    """Раз в минуту проверяем, не пора ли отправить сводку. Переживает сон ноутбука и перезапуски."""
    retry_at: datetime | None = None
    while True:
        current = now()
        if daily_due(current) and (retry_at is None or current >= retry_at):
            ok = await try_daily(bot, current)
            retry_at = None if ok else datetime.fromtimestamp(current.timestamp() + 600, cfg.tz)
        await asyncio.sleep(60)


def setup_logging() -> None:
    handlers: list[logging.Handler] = [
        RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr:  # при запуске через pythonw (без окна) консоли нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers
    )


def single_instance() -> socket.socket | None:
    """Два запущенных бота с одним токеном мешают друг другу, поэтому держим «замок» на локальном порту."""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47231))
    except OSError:
        return None
    return lock


COMMANDS = [
    BotCommand(command="report", description="Сводка прямо сейчас"),
    BotCommand(command="tomorrow", description="Расписание и ДЗ на завтра"),
    BotCommand(command="long", description="Задания на будущее"),
    BotCommand(command="marks", description="Оценки по всем предметам"),
    BotCommand(command="pause", description="Приостановить рассылку (каникулы)"),
    BotCommand(command="resume", description="Возобновить рассылку"),
    BotCommand(command="time", description="Время сводки (меняет владелец)"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="stop", description="Отписаться от рассылки"),
]


def make_bot() -> Bot:
    return Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))


def make_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_routers(router, guest_router)
    return dp


async def main() -> None:
    setup_logging()
    lock = single_instance()
    if lock is None:
        log.error("Бот уже запущен, второй экземпляр не нужен")
        return
    if not cfg.allowed_users:
        log.warning("ALLOWED_USERS пуст: бот никому не ответит. Напиши боту, он пришлёт твой ID.")
    bot = make_bot()
    dp = make_dispatcher()
    await bot.set_my_commands(COMMANDS)
    loop_task = asyncio.create_task(daily_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        loop_task.cancel()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
