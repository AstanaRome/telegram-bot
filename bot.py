import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
BUTTON_ADD = "➕ Добавить вручную"
BUTTON_EDIT = "✏️ Редактировать"
BUTTON_DELETE = "🗑 Удалить"
BUTTON_CANCEL = "❌ Отмена"

TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Almaty"))
DATETIME_FORMAT = "%Y-%m-%d %H:%M"

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


def parse_user_datetime(raw: str) -> datetime:
    try:
        naive = datetime.strptime(raw, DATETIME_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"Не удалось распознать дату '{raw}'. "
            f"Используй формат YYYY-MM-DD HH:MM (например, 2024-05-18 09:00)."
        ) from exc
    return naive.replace(tzinfo=TIMEZONE)


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


def insert_manual_session(user_id: int, start_at: datetime, end_at: datetime) -> int:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (user_id, start_at, end_at)
                VALUES (%s, %s, %s)
                RETURNING id;
                """,
                (user_id, start_at, end_at),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def update_session_times(
    session_id: int, user_id: int, start_at: datetime, end_at: datetime
) -> bool:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET start_at = %s, end_at = %s
                WHERE id = %s AND user_id = %s;
                """,
                (start_at, end_at, session_id, user_id),
            )
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def delete_session_record(session_id: int, user_id: int) -> bool:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM sessions
                WHERE id = %s AND user_id = %s;
                """,
                (session_id, user_id),
            )
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def fetch_week_sessions(
    user_id: int, week_start: datetime, week_end: datetime
) -> List[Tuple[int, datetime, Optional[datetime]]]:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, start_at, end_at
                FROM sessions
                WHERE user_id = %s
                  AND start_at >= %s
                  AND start_at < %s
                ORDER BY start_at;
                """,
                (user_id, week_start, week_end),
            )
            rows = cur.fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def calc_week_summary(
    user_id: int, week_start: datetime, week_end: datetime
) -> Tuple[float, List[str]]:
    sessions = fetch_week_sessions(user_id, week_start, week_end)
    now = current_dt()
    total_seconds = 0.0
    detail_lines: List[str] = []

    for idx, (session_id, start_at, end_at) in enumerate(sessions, start=1):
        start_local = start_at.astimezone(TIMEZONE)
        end_local = end_at.astimezone(TIMEZONE) if end_at else now
        effective_end_local = min(end_local, week_end)
        duration_seconds = max(
            0.0, (effective_end_local - start_local).total_seconds()
        )
        total_seconds += duration_seconds
        if end_at:
            detail_lines.append(
                f"{idx}. #{session_id} {format_dt(start_at)} – {format_dt(end_at)} "
                f"({format_duration(duration_seconds)})"
            )
        else:
            detail_lines.append(
                f"{idx}. #{session_id} {format_dt(start_at)} – … "
                f"(идёт, {format_duration(duration_seconds)})"
            )

    return total_seconds, detail_lines


def build_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BUTTON_START, BUTTON_STOP],
            [BUTTON_STATS, BUTTON_CURRENT],
            [BUTTON_ADD, BUTTON_EDIT, BUTTON_DELETE],
            [BUTTON_CANCEL],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я учитываю учебное время. "
        "Нажми «Начать сессию», чтобы запустить таймер.",
        reply_markup=build_keyboard(),
    )


def reset_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("flow", None)


def start_flow(context: ContextTypes.DEFAULT_TYPE, flow_type: str) -> Dict[str, Any]:
    flow: Dict[str, Any] = {"type": flow_type, "step": "init", "data": {}}
    context.user_data["flow"] = flow
    return flow


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    flow = context.user_data.get("flow")
    cancel_triggered = text.lower() == "отмена" or text == BUTTON_CANCEL

    if flow:
        if cancel_triggered:
            reset_flow(context)
            await update.message.reply_text(
                "Ввод отменён.", reply_markup=build_keyboard()
            )
            return
        flow_type = flow.get("type")
        if flow_type == "add":
            await process_add_flow(update, context, text)
            return
        if flow_type == "edit":
            await process_edit_flow(update, context, text)
            return
        if flow_type == "delete":
            await process_delete_flow(update, context, text)
            return

    if cancel_triggered:
        await update.message.reply_text(
            "Нет активного ввода, нечего отменять.", reply_markup=build_keyboard()
        )
        return

    if text == BUTTON_START:
        await handle_start_session(update)
    elif text == BUTTON_STOP:
        await handle_stop_session(update)
    elif text == BUTTON_STATS:
        await handle_stats(update)
    elif text == BUTTON_CURRENT:
        await handle_current_session(update)
    elif text == BUTTON_ADD:
        flow = start_flow(context, "add")
        flow["step"] = "start"
        await update.message.reply_text(
            "Введи начало новой сессии (формат YYYY-MM-DD HH:MM).",
        )
    elif text == BUTTON_EDIT:
        flow = start_flow(context, "edit")
        flow["step"] = "id"
        await update.message.reply_text(
            "Введи ID сессии, которую нужно отредактировать (смотри /stats).",
        )
    elif text == BUTTON_DELETE:
        flow = start_flow(context, "delete")
        flow["step"] = "id"
        await update.message.reply_text(
            "Введи ID сессии, которую нужно удалить (смотри /stats).",
        )
    else:
        await update.message.reply_text(
            "Используй кнопки или команды /add_session, /edit_session, /delete_session.",
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


def _expect_args(message: str) -> str:
    return (
        f"{message}\nФормат: YYYY-MM-DD HH:MM. Пример: 2024-05-18 09:00."
    )


async def cmd_add_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            _expect_args(
                "Нужно указать начало и конец: /add_session 2024-05-18 09:00 2024-05-18 11:00"
            )
        )
        return

    start_raw = " ".join(args[:2])
    end_raw = " ".join(args[2:4])
    try:
        start_at = parse_user_datetime(start_raw)
        end_at = parse_user_datetime(end_raw)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    if end_at <= start_at:
        await update.message.reply_text("Время окончания должно быть позже начала.")
        return

    new_id = insert_manual_session(user.id, start_at, end_at)
    await update.message.reply_text(
        "Сессия добавлена вручную.\n"
        f"ID #{new_id}\n"
        f"Начало: {format_dt(start_at)}\n"
        f"Конец: {format_dt(end_at)}\n"
        f"Длительность: {format_duration((end_at - start_at).total_seconds())}"
    )


async def cmd_edit_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    args = context.args
    if len(args) < 5:
        await update.message.reply_text(
            _expect_args(
                "Нужно указать id, новое начало и конец: "
                "/edit_session 123 2024-05-18 09:00 2024-05-18 11:30"
            )
        )
        return

    try:
        session_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    start_raw = " ".join(args[1:3])
    end_raw = " ".join(args[3:5])
    try:
        start_at = parse_user_datetime(start_raw)
        end_at = parse_user_datetime(end_raw)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    if end_at <= start_at:
        await update.message.reply_text("Время окончания должно быть позже начала.")
        return

    updated = update_session_times(session_id, user.id, start_at, end_at)
    if not updated:
        await update.message.reply_text(
            "Сессия не найдена. Проверь ID (его можно посмотреть в /stats)."
        )
        return

    await update.message.reply_text(
        "Сессия обновлена.\n"
        f"ID #{session_id}\n"
        f"Начало: {format_dt(start_at)}\n"
        f"Конец: {format_dt(end_at)}"
    )


async def cmd_delete_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Укажи ID: /delete_session 123. "
            "ID можно посмотреть в /stats."
        )
        return

    try:
        session_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    deleted = delete_session_record(session_id, user.id)
    if not deleted:
        await update.message.reply_text(
            "Сессия не найдена или уже удалена."
        )
        return

    await update.message.reply_text(f"Сессия #{session_id} удалена.")


async def process_add_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    flow = context.user_data["flow"]
    step = flow.get("step")
    data = flow.setdefault("data", {})

    if step == "start":
        try:
            start_at = parse_user_datetime(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        data["start"] = start_at
        flow["step"] = "end"
        await update.message.reply_text(
            "Начало записал. Теперь введи конец (YYYY-MM-DD HH:MM)."
        )
        return

    if step == "end":
        start_at: datetime = data.get("start")
        try:
            end_at = parse_user_datetime(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        if not start_at:
            reset_flow(context)
            await update.message.reply_text(
                "Что-то пошло не так. Попробуй начать снова.", reply_markup=build_keyboard()
            )
            return
        if end_at <= start_at:
            await update.message.reply_text("Время окончания должно быть позже начала.")
            return
        new_id = insert_manual_session(update.effective_user.id, start_at, end_at)
        reset_flow(context)
        await update.message.reply_text(
            "Сессия добавлена вручную.\n"
            f"ID #{new_id}\n"
            f"Начало: {format_dt(start_at)}\n"
            f"Конец: {format_dt(end_at)}\n"
            f"Длительность: {format_duration((end_at - start_at).total_seconds())}",
            reply_markup=build_keyboard(),
        )
        return


async def process_edit_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    flow = context.user_data["flow"]
    step = flow.get("step")
    data = flow.setdefault("data", {})

    if step == "id":
        try:
            session_id = int(text)
        except ValueError:
            await update.message.reply_text("ID должен быть числом.")
            return
        data["id"] = session_id
        flow["step"] = "start"
        await update.message.reply_text(
            "Введи новое время начала (YYYY-MM-DD HH:MM)."
        )
        return

    if step == "start":
        try:
            start_at = parse_user_datetime(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        data["start"] = start_at
        flow["step"] = "end"
        await update.message.reply_text(
            "Начало записал. Теперь введи новое время окончания."
        )
        return

    if step == "end":
        try:
            end_at = parse_user_datetime(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        start_at: Optional[datetime] = data.get("start")
        session_id: Optional[int] = data.get("id")
        if start_at is None or session_id is None:
            reset_flow(context)
            await update.message.reply_text(
                "Что-то пошло не так. Попробуй начать снова.", reply_markup=build_keyboard()
            )
            return
        if end_at <= start_at:
            await update.message.reply_text("Время окончания должно быть позже начала.")
            return
        updated = update_session_times(
            session_id, update.effective_user.id, start_at, end_at
        )
        reset_flow(context)
        if not updated:
            await update.message.reply_text(
                "Сессия не найдена. Проверь ID в /stats.",
                reply_markup=build_keyboard(),
            )
            return
        await update.message.reply_text(
            "Сессия обновлена.\n"
            f"ID #{session_id}\n"
            f"Начало: {format_dt(start_at)}\n"
            f"Конец: {format_dt(end_at)}",
            reply_markup=build_keyboard(),
        )
        return


async def process_delete_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    flow = context.user_data["flow"]
    step = flow.get("step")
    if step != "id":
        reset_flow(context)
        await update.message.reply_text(
            "Что-то пошло не так. Попробуй снова удалить через кнопку.",
            reply_markup=build_keyboard(),
        )
        return
    try:
        session_id = int(text)
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    deleted = delete_session_record(session_id, update.effective_user.id)
    reset_flow(context)
    if not deleted:
        await update.message.reply_text(
            "Сессия не найдена или уже удалена.",
            reply_markup=build_keyboard(),
        )
        return
    await update.message.reply_text(
        f"Сессия #{session_id} удалена.",
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
    app.add_handler(CommandHandler("add_session", cmd_add_session))
    app.add_handler(CommandHandler("edit_session", cmd_edit_session))
    app.add_handler(CommandHandler("delete_session", cmd_delete_session))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
