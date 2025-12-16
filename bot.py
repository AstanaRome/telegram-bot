import os
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BUTTON_START = "▶️ Начать сессию"
BUTTON_STOP = "⏹ Закончить сессию"
BUTTON_STATS = "📊 Статистика недели"
BUTTON_CURRENT = "⏱ Текущая сессия"

TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Almaty"))

POOL: Optional[ConnectionPool] = None


def current_dt() -> datetime:
    return datetime.now(tz=TIMEZONE)


def get_week_window(moment: datetime) -> tuple[datetime, datetime]:
    week_start = moment - timedelta(days=moment.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    return week_start, week_end


def get_pool() -> ConnectionPool:
    if POOL is None:
        raise RuntimeError("База данных не инициализирована.")
    return POOL


def format_dt(value: datetime) -> str:
    return value.astimezone(TIMEZONE).strftime("%d.%m %H:%M:%S")


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def init_db(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    start_at TIMESTAMPTZ NOT NULL,
                    end_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sessions_user_start
                    ON sessions (user_id, start_at);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sessions_active
                    ON sessions (user_id)
                    WHERE end_at IS NULL;
                """
            )
        conn.commit()


def fetch_active_session(user_id: int) -> Optional[Tuple[int, datetime]]:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, start_at
                FROM sessions
                WHERE user_id = %s AND end_at IS NULL
                ORDER BY start_at DESC
                LIMIT 1;
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row:
                session_id, start_at = row
                return session_id, start_at
    return None


def create_session(user_id: int, started_at: datetime) -> None:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (user_id, start_at)
                VALUES (%s, %s);
                """,
                (user_id, started_at),
            )
        conn.commit()


def close_session(session_id: int, finished_at: datetime) -> None:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET end_at = %s
                WHERE id = %s;
                """,
                (finished_at, session_id),
            )
        conn.commit()


def fetch_week_sessions(
    user_id: int, week_start: datetime, week_end: datetime
) -> List[Tuple[datetime, Optional[datetime]]]:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT start_at, end_at
                FROM sessions
                WHERE user_id = %s
                  AND start_at >= %s
                  AND start_at < %s
                ORDER BY start_at;
                """,
                (user_id, week_start, week_end),
            )
            rows = cur.fetchall()
    return [(row[0], row[1]) for row in rows]


def calc_week_summary(
    user_id: int, week_start: datetime, week_end: datetime
) -> Tuple[float, List[str]]:
    sessions = fetch_week_sessions(user_id, week_start, week_end)
    now = current_dt()
    total_seconds = 0.0
    detail_lines: List[str] = []

    for idx, (start_at, end_at) in enumerate(sessions, start=1):
        start_local = start_at.astimezone(TIMEZONE)
        end_local = end_at.astimezone(TIMEZONE) if end_at else now
        effective_end_local = min(end_local, week_end)
        duration_seconds = max(
            0.0, (effective_end_local - start_local).total_seconds()
        )
        total_seconds += duration_seconds
        if end_at:
            detail_lines.append(
                f"{idx}. {format_dt(start_at)} – {format_dt(end_at)} "
                f"({format_duration(duration_seconds)})"
            )
        else:
            detail_lines.append(
                f"{idx}. {format_dt(start_at)} – … "
                f"(идёт, {format_duration(duration_seconds)})"
            )

    return total_seconds, detail_lines


def build_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_START, BUTTON_STOP], [BUTTON_STATS, BUTTON_CURRENT]],
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
    elif text == BUTTON_CURRENT:
        await handle_current_session(update)
    else:
        await update.message.reply_text(
            "Используй кнопки на клавиатуре.",
            reply_markup=build_keyboard(),
        )


async def handle_start_session(update: Update) -> None:
    user = update.effective_user
    if not user:
        return

    active = fetch_active_session(user.id)
    if active:
        session_id, start_time = active
        now = current_dt()
        close_session(session_id, now)
        duration_seconds = (now - start_time).total_seconds()
        await update.message.reply_text(
            "Предыдущая сессия была автоматически завершена, "
            f"потому что ты забыл нажать стоп.\n"
            f"Завершено в {format_dt(now)}, длительность {format_duration(duration_seconds)}."
        )

    now = current_dt()
    create_session(user.id, now)
    await update.message.reply_text(
        f"Старт! Засёк время в {format_dt(now)}."
    )


async def handle_stop_session(update: Update) -> None:
    user = update.effective_user
    if not user:
        return

    active = fetch_active_session(user.id)
    if not active:
        await update.message.reply_text(
            "Нет активной сессии. Нажми «Начать сессию».",
        )
        return

    session_id, start_time = active
    end_time = current_dt()
    close_session(session_id, end_time)

    duration_seconds = (end_time - start_time).total_seconds()
    await update.message.reply_text(
        "Сессия завершена.\n"
        f"Начало: {format_dt(start_time)}\n"
        f"Конец: {format_dt(end_time)}\n"
        f"Длительность: {format_duration(duration_seconds)}",
    )


async def handle_stats(update: Update) -> None:
    user = update.effective_user
    if not user:
        return

    now = current_dt()
    week_start, week_end = get_week_window(now)
    total_seconds, session_lines = calc_week_summary(user.id, week_start, week_end)
    total_int = int(total_seconds)
    hours, remainder = divmod(total_int, 3600)
    minutes, seconds = divmod(remainder, 60)
    total_formatted = f"{hours:02}:{minutes:02}:{seconds:02}"

    msg = (
        "Статистика за неделю\n"
        f"{week_start:%d.%m} – {week_end - timedelta(days=1):%d.%m}\n"
        f"Всего: {total_formatted} (ч:м:с)"
    )
    if session_lines:
        msg += "\n\nСессии:\n" + "\n".join(session_lines)
    else:
        msg += "\n\nЗа эту неделю сессий ещё не было."
    await update.message.reply_text(msg, reply_markup=build_keyboard())


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_stop_session(update)


async def handle_current_session(update: Update) -> None:
    user = update.effective_user
    if not user:
        return

    active = fetch_active_session(user.id)
    if not active:
        await update.message.reply_text(
            "Сейчас нет активной сессии.",
            reply_markup=build_keyboard(),
        )
        return

    _, start_time = active
    now = current_dt()
    duration_seconds = (now - start_time).total_seconds()
    await update.message.reply_text(
        "Сессия идёт.\n"
        f"Старт: {format_dt(start_time)}\n"
        f"Прошло: {format_duration(duration_seconds)}",
        reply_markup=build_keyboard(),
    )


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не установлен.")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL не установлен.")

    global POOL
    POOL = ConnectionPool(db_url, min_size=1, max_size=5)
    init_db(POOL)

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    app.run_polling()


if __name__ == "__main__":
    main()
