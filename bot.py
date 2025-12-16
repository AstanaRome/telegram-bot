import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DATA_DIR = Path("data")
DATA_FILE = DATA_DIR / "sessions.json"

BUTTON_START = "▶️ Начать сессию"
BUTTON_STOP = "⏹ Закончить сессию"
BUTTON_STATS = "📊 Статистика недели"

TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))


@dataclass
class Session:
    start: datetime
    end: Optional[datetime] = None

    def to_dict(self) -> Dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else "",
        }

    @staticmethod
    def from_dict(data: Dict[str, str]) -> "Session":
        end = datetime.fromisoformat(data["end"]) if data.get("end") else None
        return Session(start=datetime.fromisoformat(data["start"]), end=end)


def ensure_storage() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text(json.dumps({"users": {}}, ensure_ascii=False, indent=2))


def load_data() -> Dict[str, Any]:
    ensure_storage()
    with DATA_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_data(data: Dict[str, Any]) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def get_user_record(data: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    users = data.setdefault("users", {})
    return users.setdefault(str(user_id), {"sessions": [], "active_session": None})


def current_dt() -> datetime:
    return datetime.now(tz=TIMEZONE)


def get_week_window(moment: datetime) -> tuple[datetime, datetime]:
    week_start = moment - timedelta(days=moment.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    return week_start, week_end


def calc_week_minutes(sessions: List[Dict[str, str]]) -> int:
    now = current_dt()
    week_start, week_end = get_week_window(now)
    total_seconds = 0
    for record in sessions:
        session = Session.from_dict(record)
        if not session.end:
            continue
        if not (week_start <= session.start < week_end):
            continue
        total_seconds += (session.end - session.start).total_seconds()
    return int(total_seconds // 60)


def build_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_START, BUTTON_STOP], [BUTTON_STATS]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я учитываю учебное время. "
        "Нажми «Начать сессию», чтобы запустить таймер.",
        reply_markup=build_keyboard(),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if text == BUTTON_START:
        await handle_start_session(update)
    elif text == BUTTON_STOP:
        await handle_stop_session(update)
    elif text == BUTTON_STATS:
        await handle_stats(update)
    else:
        await update.message.reply_text(
            "Используй кнопки на клавиатуре.",
            reply_markup=build_keyboard(),
        )


async def handle_start_session(update: Update) -> None:
    data = load_data()
    user_data = get_user_record(data, update.effective_user.id)
    if user_data.get("active_session"):
        start_time = datetime.fromisoformat(user_data["active_session"])
        await update.message.reply_text(
            f"Сессия уже идёт с {start_time.strftime('%H:%M:%S')}."
        )
        return

    now = current_dt()
    user_data["active_session"] = now.isoformat()
    save_data(data)
    await update.message.reply_text(
        f"Старт! Засёк время в {now.strftime('%H:%M')}."
    )


async def handle_stop_session(update: Update) -> None:
    data = load_data()
    user_data = get_user_record(data, update.effective_user.id)
    active = user_data.get("active_session")
    if not active:
        await update.message.reply_text(
            "Нет активной сессии. Нажми «Начать сессию».",
        )
        return

    start_time = datetime.fromisoformat(active)
    end_time = current_dt()
    user_data["sessions"].append(Session(start_time, end_time).to_dict())
    user_data["active_session"] = None
    save_data(data)

    duration_minutes = int((end_time - start_time).total_seconds() // 60)
    await update.message.reply_text(
        f"Сессия завершена. Длительность: {duration_minutes} мин.",
    )


async def handle_stats(update: Update) -> None:
    data = load_data()
    user_data = get_user_record(data, update.effective_user.id)
    total_minutes = calc_week_minutes(user_data["sessions"])
    hours, minutes = divmod(total_minutes, 60)
    week_start, week_end = get_week_window(current_dt())

    msg = (
        "Статистика за неделю\n"
        f"{week_start:%d.%m} – {week_end - timedelta(days=1):%d.%m}\n"
        f"Всего: {hours} ч {minutes} мин"
    )
    await update.message.reply_text(msg, reply_markup=build_keyboard())


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_stop_session(update)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не установлен.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    app.run_polling()


if __name__ == "__main__":
    main()
