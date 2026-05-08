"""
Fitness Bot — simplified version.
Single chat interface with full context to Claude.
No conversation handlers, no state machines — Claude handles everything.
"""
import logging
import os
import json
import tempfile
import asyncio
from datetime import datetime, date, timedelta, time as dtime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
from database import Database
from ai_handler import AIHandler
from monitor import ProactiveMonitor
from data_import import (
    parse_apple_health_zip, save_apple_health_data,
    parse_fatsecret_csv, save_fatsecret_data
)
from analytics import (
    generate_weight_chart, generate_kbju_chart, generate_correlation_chart
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

db = Database()
ai = AIHandler(ANTHROPIC_API_KEY)


def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 План дня"), KeyboardButton("📅 План недели")],
        [KeyboardButton("📈 Аналитика")],
    ], resize_keyboard=True)

def _md_escape(text: str) -> str:
    """Escape markdown special chars in user content"""
    if not isinstance(text, str):
        return str(text)
    # Escape characters that break Telegram markdown
    return text.replace('_', r'\_').replace('*', r'\*').replace('[', r'\[').replace('`', r'\`')


# ============= COMMANDS =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    db.ensure_user(user_id)

    plan = db.get_nutrition_plan(user_id)
    if not plan:
        await update.message.reply_text(
            f"Привет, {name}! 👋\n\n"
            "Я твой персональный фитнес-тренер.\n\n"
            "📸 Принимаю фото еды, скрины FatSecret, отчёты InBody (фото или PDF)\n"
            "🎤 Понимаю голосовые\n"
            "💬 Веду план питания, тренировок, таблеток и задач\n\n"
            "Чтобы начать — расскажи о себе: пол, возраст, рост, вес, цель. "
            "Можешь скинуть отчёт InBody — будет точнее.",
            reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            f"С возвращением, {name}! 💪",
            reply_markup=main_keyboard()
        )



async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tell user to send Apple Health zip or FatSecret CSV"""
    context.user_data['waiting_import'] = True
    await update.message.reply_text(
        "📥 *Импорт исторических данных*\n\n"
        "Можешь прислать:\n"
        "📦 *Apple Health* — выгрузи ZIP из приложения Здоровье "
        "(значок профиля → Экспортировать данные)\n\n"
        "📊 *FatSecret CSV* — выгрузи дневник питания "
        "(будут импортированы последние 12 месяцев, дни вне диапазона "
        "1700-2700 ккал отфильтруются как недостоверные)\n\n"
        "Жду файл...",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


async def show_day_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's plan as text"""
    user_id = update.effective_user.id
    today_str = date.today().isoformat()

    day_plan = db.get_effective_day_plan(user_id)
    totals = db.get_today_totals(user_id) or {}
    meals_eaten = db.get_today_meals(user_id) or []
    plan = db.get_nutrition_plan(user_id) or {}
    supps = db.get_supplements(user_id) or []
    taken = db.get_supplements_taken_today(user_id) or []
    tasks = db.get_tasks_today(user_id) or []
    done_ids = db.get_tasks_done_today(user_id) or []
    weight = db.get_latest_weight(user_id)
    activity = db.get_today_activity(user_id) or {}

    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    today_name = days_ru[datetime.now().weekday()]

    lines = [f"📋 *План на {today_name}, {datetime.now().strftime('%d.%m')}*\n"]

    # Stats
    if weight:
        lines.append(f"⚖️ Вес: {weight} кг")
    if activity.get('steps'):
        lines.append(f"👟 Шаги: {activity['steps']:,}")

    # Macros
    cal = totals.get('calories', 0)
    plan_cal = plan.get('calories', 0)
    if plan_cal:
        pct = int(cal / plan_cal * 100)
        bar_len = 12
        filled = min(bar_len, int(pct / 100 * bar_len))
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"\n🔥 *{cal} / {plan_cal} ккал* ({pct}%)")
        lines.append(f"`{bar}`")
        lines.append(
            f"Б: {totals.get('protein',0)}/{plan.get('protein',0)}г · "
            f"Ж: {totals.get('fat',0)}/{plan.get('fat',0)}г · "
            f"У: {totals.get('carbs',0)}/{plan.get('carbs',0)}г"
        )

    # Today's planned meals (from template/override)
    if day_plan and day_plan.get('meals'):
        lines.append("\n🍽 *Запланировано:*")
        for m in day_plan['meals']:
            lines.append(
                f"  {m.get('time','--:--')} {_md_escape(m.get('name',''))} — "
                f"{m.get('calories',0)} ккал"
            )

    # Today's workout
    if day_plan and day_plan.get('workout'):
        w = day_plan['workout']
        lines.append(f"\n💪 *Тренировка:* {w.get('name','')} в {w.get('time','--:--')}")

    # Eaten today
    if meals_eaten:
        lines.append("\n✅ *Съедено:*")
        for m in meals_eaten:
            lines.append(f"  {m['time']} {_md_escape(m['description'])} — {m['calories']} ккал")

    # Supplements
    if supps:
        lines.append("\n💊 *Таблетки:*")
        for s in supps:
            tick = "✅" if s['id'] in taken else "⬜"
            time_str = f" {s['time_of_day']}" if s.get('time_of_day') else ""
            lines.append(f"{tick} {_md_escape(s['name'])} {_md_escape(s['dose'])}{time_str}")

    # Tasks
    if tasks:
        lines.append("\n📌 *Задачи:*")
        for t in tasks:
            tick = "✅" if t['id'] in done_ids else "⬜"
            time_str = f" {t['time_str']}" if t.get('time_str') else ""
            lines.append(f"{tick} {_md_escape(t['title'])}{time_str}")

    if not (meals_eaten or supps or tasks or (day_plan and day_plan.get('meals'))):
        lines.append("\n_План пустой. Расскажи боту что съел, что планируешь, что нужно сделать._")

    text = "\n".join(lines)
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())
    except Exception:
        await update.message.reply_text(text, reply_markup=main_keyboard())


async def show_week_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show weekly template"""
    user_id = update.effective_user.id
    template = db.get_weekly_template(user_id)
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    days_ru = {"monday": "Пн", "tuesday": "Вт", "wednesday": "Ср",
               "thursday": "Чт", "friday": "Пт", "saturday": "Сб", "sunday": "Вс"}

    if not template:
        await update.message.reply_text(
            "Недельный шаблон не составлен.\n\n"
            "Напиши боту: «составь шаблон недели» — он спросит детали и сохранит.",
            reply_markup=main_keyboard()
        )
        return

    today = date.today()
    today_idx = datetime.now().weekday()
    lines = ["📅 *Шаблон недели*\n"]

    for i, day in enumerate(days):
        delta = (i - today_idx) % 7
        day_date = today + timedelta(days=delta)
        override = db.get_day_override(user_id, day_date.isoformat())
        # Use override if exists, else template
        d = override if override else template.get(day, {})
        if not d:
            continue
        meals = d.get('meals', [])
        workout = d.get('workout')
        total_cal = sum(m.get('calories', 0) for m in meals)

        marker = "🔄" if override else ""
        is_today = "👉" if i == today_idx else " "

        wstr = f" 💪 {_md_escape(workout.get('name', ''))}" if workout else ""
        lines.append(f"{is_today}{marker} *{days_ru[day]}*: {total_cal} ккал{wstr}")

    lines.append("\n_🔄 — день изменён на сегодня. На следующей неделе вернётся базовый план._")
    lines.append("_Чтобы пересоставить шаблон — напиши «обнови шаблон недели»_")

    text = "\n".join(lines)
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())
    except Exception:
        await update.message.reply_text(text, reply_markup=main_keyboard())


async def show_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show analytics with charts"""
    user_id = update.effective_user.id
    plan = db.get_nutrition_plan(user_id)
    if not plan:
        await update.message.reply_text(
            "Сначала настрой план — расскажи боту о себе и целях.",
            reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text("⏳ Собираю данные и строю графики...")
    week_data = db.get_week_stats(user_id)
    month_data = db.get_month_stats(user_id)
    weight_history = db.get_weight_history(user_id, days=30)
    activity_data = db.get_activity_history(user_id, days=30)

    analysis = await ai.generate_text_analytics(
        period='month',
        week_data=week_data,
        month_data=month_data,
        plan=plan,
        weight_history=weight_history,
        activity_data=activity_data,
    )
    await update.message.reply_text(
        f"📈 *Аналитика за месяц*\n\n{analysis}",
        parse_mode="Markdown"
    )

    if weight_history:
        chart = generate_weight_chart(weight_history, "месяц")
        if chart:
            import io
            await update.message.reply_photo(
                photo=io.BytesIO(chart),
                caption=f"📉 Вес за месяц"
            )

    if month_data:
        kbju_chart = generate_kbju_chart(month_data, plan)
        if kbju_chart:
            import io
            await update.message.reply_photo(
                photo=io.BytesIO(kbju_chart),
                caption="🍽 КБЖУ по дням"
            )

    if len(weight_history) >= 5 and month_data:
        corr_chart = generate_correlation_chart(weight_history, month_data, activity_data)
        if corr_chart:
            import io
            await update.message.reply_photo(
                photo=io.BytesIO(corr_chart),
                caption="🔗 Корреляции"
            )


# ============= MEDIA HANDLERS =============

_photo_buffers = {}  # user_id -> {"photos": [bytes,...], "task": asyncio.Task, "message_ids": [...]}

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buffer photos for 3 seconds — process all together if user sends multiple"""
    user_id = update.effective_user.id

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = bytes(await file.download_as_bytearray())

    # Get or create buffer for this user
    buf = _photo_buffers.get(user_id)
    if buf is None:
        buf = {"photos": [], "task": None, "first_update": update}
        _photo_buffers[user_id] = buf

    buf["photos"].append(photo_bytes)

    # Cancel previous waiting task if exists
    if buf.get("task") and not buf["task"].done():
        buf["task"].cancel()

    # Schedule processing after 3-second pause
    async def delayed_process():
        try:
            await asyncio.sleep(3)
            current_buf = _photo_buffers.pop(user_id, None)
            if not current_buf or not current_buf["photos"]:
                return
            photos = current_buf["photos"]
            first_upd = current_buf["first_update"]

            if len(photos) == 1:
                await first_upd.message.reply_text("📸 Анализирую...")
                await _process_image(first_upd, context, photos[0])
            else:
                await first_upd.message.reply_text(f"📸 Анализирую {len(photos)} фото вместе...")
                await _process_multi_images(first_upd, context, photos)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Photo buffer error: {e}")

    buf["task"] = asyncio.create_task(delayed_process())


async def _process_multi_images(update: Update, context: ContextTypes.DEFAULT_TYPE, images: list):
    """Process multiple images together - useful for FatSecret day screenshots"""
    user_id = update.effective_user.id
    full_context = _build_full_context(user_id)
    result = await ai.analyze_multi_images(images, full_context)
    if not result:
        await update.message.reply_text(
            "Не смог проанализировать фото 🤔",
            reply_markup=main_keyboard()
        )
        return
    await _execute_intent(update, context, result)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDF, image, ZIP, CSV documents"""
    user_id = update.effective_user.id
    doc = update.message.document
    if not doc:
        return

    mime = doc.mime_type or ""
    name = (doc.file_name or "").lower()

    # Import mode — Apple Health ZIP or FatSecret CSV
    if context.user_data.get('waiting_import') or name.endswith('.zip') or name.endswith('.csv'):
        context.user_data.pop('waiting_import', None)
        await update.message.reply_text("📥 Загружаю файл...")
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())

        if name.endswith('.zip') or 'zip' in mime:
            await update.message.reply_text("📦 Парсю Apple Health (1-2 минуты)...")
            try:
                parsed = parse_apple_health_zip(file_bytes)
            except Exception as e:
                logger.error(f"Apple Health import error: {e}")
                await update.message.reply_text(f"Ошибка: {e}", reply_markup=main_keyboard())
                return

            if parsed.get('error'):
                await update.message.reply_text(f"❌ {parsed['error']}", reply_markup=main_keyboard())
                return

            anomalies = parsed.get('anomalies', [])
            dr = parsed.get('date_range') or {}
            total_days = parsed.get('total_days', 0)

            msg_lines = [
                f"📦 *Apple Health: {dr.get('from', '?')} — {dr.get('to', '?')}*",
                f"Дней с данными: {total_days}",
                f"Тренировок: {len(parsed.get('workouts', []))}",
            ]
            if parsed.get('height'):
                msg_lines.append(f"Рост: {round(parsed['height'])} см")

            if anomalies:
                msg_lines.append(f"\n⚠️ *Найдено {len(anomalies)} подозрительных значений:*")
                for a in anomalies[:15]:
                    msg_lines.append(f"  {a['date']}: {a['reason']}")
                if len(anomalies) > 15:
                    msg_lines.append(f"  ... и ещё {len(anomalies) - 15}")
                msg_lines.append("\nУдалить эти аномалии? Напиши *да* или *оставить*.")
                context.user_data['pending_apple_health'] = parsed
            else:
                msg_lines.append("\n✅ Аномалий не найдено.")
                saved = save_apple_health_data(user_id, parsed, db, remove_anomalies=False)
                msg_lines.append(
                    f"⚖️ Вес: {saved.get('weight', 0)} | 👟 Активность: {saved.get('activity', 0)} | "
                    f"💤 Сон: {saved.get('sleep', 0)} | 💪 Тренировки: {saved.get('workouts', 0)}"
                )
            msg_lines.append("\n_Пришли ещё файлы или /analyze когда закончишь._")

            text = "\n".join(msg_lines)
            try:
                await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())
            except Exception:
                await update.message.reply_text(text, reply_markup=main_keyboard())
            return

        elif name.endswith('.csv') or 'csv' in mime:
            await update.message.reply_text("📊 Парсю FatSecret CSV...")
            try:
                parsed = parse_fatsecret_csv(file_bytes, months_limit=12)
            except Exception as e:
                logger.error(f"FatSecret import error: {e}")
                await update.message.reply_text(f"Ошибка: {e}", reply_markup=main_keyboard())
                return

            if parsed.get('error'):
                await update.message.reply_text(f"❌ {parsed['error']}", reply_markup=main_keyboard())
                return

            valid_days = parsed.get('valid_days', {})
            skipped = parsed.get('skipped_days', [])

            msg_lines = [
                f"📊 *FatSecret CSV*",
                f"Всего строк: {parsed.get('total_rows', 0)}",
                f"✅ Валидных дней (1700-2700 ккал): {len(valid_days)}",
            ]

            if skipped:
                msg_lines.append(f"\n⚠️ *Дни вне диапазона ({len(skipped)}):*")
                for s in skipped[:20]:
                    msg_lines.append(f"  {s['date']}: {s['total_cal']} ккал")
                if len(skipped) > 20:
                    msg_lines.append(f"  ... и ещё {len(skipped) - 20}")
                msg_lines.append("\nУдалить эти дни как недостоверные? Напиши *да* или *оставить*.")
                context.user_data['pending_fatsecret'] = parsed
            else:
                saved = save_fatsecret_data(user_id, parsed, db)
                msg_lines.append(
                    f"\n🍽 Записано приёмов: {saved['saved_meals']}\n"
                    f"📦 Продуктов в базе: {saved['saved_products']}"
                )

            msg_lines.append("\n_Пришли ещё файлы или /analyze когда закончишь._")

            text = "\n".join(msg_lines)
            try:
                await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())
            except Exception:
                await update.message.reply_text(text, reply_markup=main_keyboard())
            return

            if result.get('error'):
                await update.message.reply_text(f"❌ {result['error']}", reply_markup=main_keyboard())
                return

            msg = (
                f"✅ *FatSecret импортирован!*\n\n"
                f"📋 Строк обработано: {result.get('parsed_rows', 0)}\n"
                f"✅ Дней с валидными данными: {result.get('valid_days', 0)}\n"
                f"⚠️ Отфильтровано (вне 1700-2700 ккал): {result.get('skipped_days_out_of_range', 0)}\n"
                f"🍽 Записано приёмов пищи: {result.get('saved_meals', 0)}\n"
                f"📦 Добавлено продуктов в базу: {result.get('saved_products', 0)}\n\n"
                f"_Анализирую данные..._"
            )
            try:
                await update.message.reply_text(msg, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(msg)

            await update.message.reply_text(
                "Можешь прислать ещё файлы или напиши *«проанализируй»* — "
                "когда закончишь импорт, запущу анализ всех данных одним запросом.",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )
            return

    if "pdf" not in mime and "image" not in mime:
        await update.message.reply_text(
            "Умею читать только PDF и изображения. "
            "Скинь скриншот или фото.",
            reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text("📄 Читаю файл...")
    file = await context.bot.get_file(doc.file_id)
    file_bytes = bytes(await file.download_as_bytearray())

    if "pdf" in mime:
        try:
            import fitz
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            page = pdf_doc[0]
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("jpeg")
            pdf_doc.close()
        except Exception as e:
            logger.error(f"PDF error: {e}")
            await update.message.reply_text(
                "Не смог прочитать PDF. Попробуй сделать скриншот.",
                reply_markup=main_keyboard()
            )
            return
    else:
        img_bytes = file_bytes

    await _process_image(update, context, img_bytes)




async def analyze_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger AI analysis of imported historical data"""
    await update.message.reply_text("🔍 Анализирую историю...")
    await _post_import_analysis(update, context)


async def _post_import_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After import - one AI call to find anomalies and ask questions"""
    user_id = update.effective_user.id
    summary = db.get_import_summary(user_id)
    monthly = db.get_monthly_aggregates(user_id, months=12)

    if not monthly:
        return

    full_context = _build_full_context(user_id)
    full_context['import_summary'] = summary
    full_context['monthly_aggregates'] = monthly

    prompt = (
        "Я только что импортировал свою историю из Apple Health и/или FatSecret. "
        "Посмотри на месячные сводки в `monthly_aggregates`. "
        "Найди аномалии — где вес заметно менялся, были ли периоды с очень разной средней калорийностью. "
        "Задай мне 1-3 уточняющих вопроса о таких периодах, чтобы понять контекст. "
        "Например: 'Вижу с марта 2025 ты резко начал худеть, что изменилось?' "
        "Если аномалий нет — просто дай краткую оценку периода."
    )

    try:
        result = await ai.process_message(prompt, full_context, [])
        await _execute_intent(update, context, result)
    except Exception as e:
        logger.error(f"Post-import analysis error: {e}")


async def _process_image(update: Update, context: ContextTypes.DEFAULT_TYPE, img_bytes: bytes):
    """Unified image processing - Claude decides what it is"""
    user_id = update.effective_user.id
    full_context = _build_full_context(user_id)

    result = await ai.analyze_image_unified(img_bytes, full_context)

    if not result:
        await update.message.reply_text(
            "Не смог разобрать изображение 🤔 Попробуй текстом или другим фото.",
            reply_markup=main_keyboard()
        )
        return

    # Result contains intent + reply text
    await _execute_intent(update, context, result)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe and process as text"""
    user_id = update.effective_user.id
    await update.message.reply_text("🎤 Слушаю...")

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        audio_bytes = await file.download_as_bytearray()

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        text = await ai.transcribe_voice(tmp_path)
        os.unlink(tmp_path)

        if not text:
            await update.message.reply_text(
                "Не разобрал голосовое. Напиши текстом или попробуй ещё раз.",
                reply_markup=main_keyboard()
            )
            return

        await update.message.reply_text(f"🎤 _{text}_", parse_mode="Markdown")

        # Process transcribed text directly via AI
        if 'chat_history' not in context.user_data:
            context.user_data['chat_history'] = []
        context.user_data['chat_history'].append({"role": "user", "content": text})
        if len(context.user_data['chat_history']) > 40:
            context.user_data['chat_history'] = context.user_data['chat_history'][-40:]

        full_context = _build_full_context(user_id)
        chat_history = context.user_data['chat_history']

        await update.message.chat.send_action("typing")
        try:
            result = await ai.process_message(text, full_context, chat_history[:-1])
        except Exception as e:
            logger.error(f"AI error: {e}")
            await update.message.reply_text(
                "Что-то пошло не так. Попробуй ещё раз.",
                reply_markup=main_keyboard()
            )
            return

        await _execute_intent(update, context, result)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(
            "Ошибка обработки голосового.",
            reply_markup=main_keyboard()
        )


# ============= TEXT HANDLER =============

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """All text messages go to Claude with full context and history"""
    user_id = update.effective_user.id
    text = update.message.text

    # Buttons
    if text == "📋 План дня":
        await show_day_plan(update, context)
        return
    if text == "📅 План недели":
        await show_week_plan(update, context)
        return
    if text == "📈 Аналитика":
        await show_analytics(update, context)
        return

    # Maintain chat history (40 messages = 20 exchanges)
    if 'chat_history' not in context.user_data:
        context.user_data['chat_history'] = []
    context.user_data['chat_history'].append({"role": "user", "content": text})
    if len(context.user_data['chat_history']) > 40:
        context.user_data['chat_history'] = context.user_data['chat_history'][-40:]

    full_context = _build_full_context(user_id)
    chat_history = context.user_data['chat_history']

    await update.message.chat.send_action("typing")

    try:
        result = await ai.process_message(text, full_context, chat_history[:-1])
    except Exception as e:
        logger.error(f"AI error: {e}")
        await update.message.reply_text(
            "Что-то пошло не так. Попробуй ещё раз.",
            reply_markup=main_keyboard()
        )
        return

    await _execute_intent(update, context, result)


async def _execute_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    """Execute actions returned by Claude and send reply"""
    user_id = update.effective_user.id
    actions = result.get('actions', [])
    reply = result.get('reply', '')

    # Execute all actions
    for action in actions:
        atype = action.get('type')
        data = action.get('data', {})

        try:
            if atype == 'meal':
                db.log_meal(user_id, data)
                # Auto-add to product database
                if data.get('description'):
                    db.add_or_update_product(user_id, data)

            elif atype == 'meals_replace_today':
                # Used when user sends a full day report from FatSecret
                db.clear_today_meals(user_id)
                for m in data.get('meals', []):
                    db.log_meal(user_id, m)
                    if m.get('description'):
                        db.add_or_update_product(user_id, m)

            elif atype == 'plan_meal':
                # Add meal to today's plan (override)
                today_str = date.today().isoformat()
                override = db.get_day_override(user_id, today_str) or db.get_effective_day_plan(user_id) or {}
                override = dict(override)
                meals = list(override.get('meals', []))
                meals.append(data)
                override['meals'] = meals
                db.save_day_override(user_id, today_str, override)

            elif atype == 'workout':
                db.log_workout(user_id, data)

            elif atype == 'weight':
                db.log_weight(user_id, data['weight'])

            elif atype == 'activity':
                db.log_activity(user_id, data)

            elif atype == 'sleep':
                db.log_sleep(user_id, data.get('hours', 0), data.get('quality'))

            elif atype == 'add_supplement':
                db.save_supplement(
                    user_id,
                    name=data.get('name', ''),
                    dose=data.get('dose', ''),
                    timing=data.get('timing', 'independent'),
                    time_of_day=data.get('time_of_day')
                )

            elif atype == 'mark_supplement_taken':
                # Find supplement by name
                supps = db.get_supplements(user_id)
                for s in supps:
                    if s['name'].lower() == data.get('name', '').lower():
                        db.log_supplement_taken(user_id, s['id'])
                        break

            elif atype == 'add_task':
                db.save_task(user_id, data.get('title', ''), data.get('time_str'), data.get('repeat', 'none'))

            elif atype == 'mark_task_done':
                tasks = db.get_tasks_today(user_id)
                for t in tasks:
                    if t['title'].lower() == data.get('title', '').lower():
                        db.log_task_done(user_id, t['id'])
                        break

            elif atype == 'update_nutrition_plan':
                db.save_nutrition_plan(user_id, data, is_base=True)

            elif atype == 'update_profile':
                db.save_user_profile(user_id, data)
                if data.get('weight'):
                    db.log_weight(user_id, data['weight'])

            elif atype == 'update_day_today':
                # Edit only today's plan (override)
                today_str = date.today().isoformat()
                db.save_day_override(user_id, today_str, data)

            elif atype == 'update_day_for_date':
                # Edit specific future day (within current week or next few days)
                target_date = data.get('date')
                day_data = data.get('plan') or data.get('day_plan') or {}
                if target_date:
                    db.save_day_override(user_id, target_date, day_data)

            elif atype == 'update_weekly_template':
                # Update full week template (only with explicit user permission)
                db.save_weekly_template(user_id, data)

            elif atype == 'save_inbody':
                db.save_inbody(user_id, data)

            elif atype == 'add_product':
                db.add_or_update_product(user_id, data)

            elif atype == 'mark_product_always':
                db.mark_product_group(user_id, data.get('name', ''), 'always')

            elif atype == 'mark_product_frequent':
                db.mark_product_group(user_id, data.get('name', ''), 'frequent')

            elif atype == 'mark_product_oneoff':
                db.mark_product_group(user_id, data.get('name', ''), 'oneoff')

            elif atype == 'set_workout_schedule':
                db.save_workout_schedule(user_id, data)

        except Exception as e:
            logger.error(f"Action {atype} error: {e}")

    # Save assistant reply to history
    if reply:
        if 'chat_history' in context.user_data:
            context.user_data['chat_history'].append({"role": "assistant", "content": reply})
        await _send_long_message(update, reply, main_keyboard())


async def _send_long_message(update: Update, text: str, reply_markup=None, max_length: int = 3500):
    """Split long messages by paragraphs/lines to avoid Telegram's 4096 limit"""
    if len(text) <= max_length:
        try:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            await update.message.reply_text(text, reply_markup=reply_markup)
        return

    # Split into chunks at paragraph boundaries
    chunks = []
    remaining = text
    while len(remaining) > max_length:
        # Try to split at paragraph
        split_at = remaining.rfind("\n\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = remaining.rfind("\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = remaining.rfind(". ", 0, max_length)
        if split_at < max_length // 2:
            split_at = max_length
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        try:
            await update.message.reply_text(
                chunk,
                parse_mode="Markdown",
                reply_markup=reply_markup if is_last else None
            )
        except Exception:
            await update.message.reply_text(
                chunk,
                reply_markup=reply_markup if is_last else None
            )


# ============= CONTEXT BUILDER =============

def _build_full_context(user_id: int) -> dict:
    """Build complete context for Claude — everything it needs to know"""
    from datetime import date, timedelta
    profile = db.get_user_profile(user_id) or {}
    plan = db.get_nutrition_plan(user_id) or {}
    schedule = db.get_workout_schedule(user_id) or {}
    weekly = db.get_weekly_template(user_id) or {}
    today_str = date.today().isoformat()
    day_override = db.get_day_override(user_id, today_str)
    effective_day = db.get_effective_day_plan(user_id) or {}

    today_totals = db.get_today_totals(user_id) or {}
    today_meals = db.get_today_meals(user_id) or []
    recent_meals = db.get_meal_history(user_id, days=7)
    recent_workouts = db.get_recent_logs(user_id, days=7).get('workouts', [])

    weight_history = db.get_weight_history(user_id, days=14)
    activity = db.get_today_activity(user_id) or {}
    sleep = db.get_last_sleep(user_id)
    inbody = db.get_latest_inbody(user_id)

    supps = db.get_supplements(user_id) or []
    supps_taken = db.get_supplements_taken_today(user_id) or []
    tasks = db.get_tasks_today(user_id) or []
    tasks_done = db.get_tasks_done_today(user_id) or []

    # Clean expired overrides
    db.cleanup_old_overrides(user_id)

    products = db.get_products_summary(user_id) or {}
    promotion_candidates = db.find_promotion_candidates(user_id) or []
    demotion_candidates = db.find_demotion_candidates(user_id) or []
    future_overrides = db.get_future_overrides(user_id, days_ahead=7) or []

    # Add yesterday's data for context
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    day_before = (date.today() - timedelta(days=2)).isoformat()
    yesterday_meals = []
    day_before_meals = []
    try:
        with db._conn() as conn:
            for d, target in [(yesterday, yesterday_meals), (day_before, day_before_meals)]:
                rows = conn.execute(
                    "SELECT description, calories, protein, fat, carbs, time FROM meal_logs WHERE user_id=? AND date=? ORDER BY time",
                    (user_id, d)
                ).fetchall()
                for r in rows:
                    target.append(dict(r))
    except Exception:
        pass

    return {
        'profile': profile,
        'nutrition_plan': plan,
        'workout_schedule': schedule,
        'weekly_template': weekly,
        'today_override': day_override,
        'effective_day_plan': effective_day,
        'today_totals': today_totals,
        'today_meals': today_meals,
        'recent_meals': recent_meals[:30],
        'recent_workouts': recent_workouts[:5],
        'weight_history': weight_history,
        'activity_today': activity,
        'last_sleep': sleep,
        'latest_inbody': inbody,
        'supplements': supps,
        'supplements_taken_today': supps_taken,
        'tasks': tasks,
        'tasks_done_today': tasks_done,
        'products': products,
        'promotion_candidates': promotion_candidates,
        'demotion_candidates': demotion_candidates,
        'future_overrides': future_overrides,
        'yesterday_meals': yesterday_meals,
        'day_before_meals': day_before_meals,
    }


# ============= MAIN =============

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    monitor = ProactiveMonitor(db=db, ai=ai, bot=app.bot)
    job_queue = app.job_queue

    if job_queue:
        job_queue.run_daily(
            monitor.run_all_checks,
            time=dtime(hour=20, minute=0),
            name="daily_monitor"
        )

        async def morning_checkin(context):
            today_str = date.today().isoformat()
            users = db.get_all_users()
            for user in users:
                uid = user['user_id']
                try:
                    if db.get_morning_checkin_done(uid, today_str):
                        continue
                    template = db.get_weekly_template(uid)
                    if not template:
                        continue
                    day_plan = db.get_effective_day_plan(uid)
                    if not day_plan:
                        continue

                    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                    today_ru = days_ru[datetime.now().weekday()]

                    meals = day_plan.get('meals', [])
                    workout = day_plan.get('workout')
                    supps_today = db.get_supplements(uid)

                    lines = [f"🌅 *Доброе утро! План на {today_ru}:*\n"]
                    if workout:
                        lines.append(f"💪 {workout.get('name','Тренировка')} в {workout.get('time','--:--')}")
                    total_cal = sum(m.get('calories', 0) for m in meals)
                    lines.append(f"\n🍽 *Питание ({total_cal} ккал):*")
                    for m in meals:
                        lines.append(f"  {m.get('time','--:--')} {m.get('name','')} — {m.get('calories',0)} ккал")
                    if supps_today:
                        lines.append("\n💊 *Таблетки:*")
                        for s in supps_today:
                            t = f" {s['time_of_day']}" if s.get('time_of_day') else ""
                            lines.append(f"  {s['name']} {s['dose']}{t}")

                    lines.append("\n_Что меняем на сегодня? Или напиши «всё по плану»_")

                    await app.bot.send_message(
                        chat_id=uid,
                        text="\n".join(lines),
                        parse_mode="Markdown"
                    )
                    db.save_morning_checkin_done(uid, today_str)
                except Exception as e:
                    logger.error(f"Morning checkin {uid}: {e}")

        job_queue.run_daily(
            morning_checkin,
            time=dtime(hour=6, minute=0),
            name="morning_checkin"
        )

        async def daily_summary(context):
            users = db.get_all_users()
            for user in users:
                uid = user['user_id']
                try:
                    plan = db.get_nutrition_plan(uid)
                    if not plan:
                        continue
                    today_meals = db.get_today_meals(uid)
                    today_totals = db.get_today_totals(uid)
                    if not today_totals or today_totals.get('calories', 0) == 0:
                        continue
                    activity = db.get_today_activity(uid)
                    recent = db.get_recent_logs(uid, days=1)
                    had_workout = bool(recent.get('workouts'))
                    weight = db.get_latest_weight(uid)
                    summary = await ai.generate_daily_summary(
                        today_meals=today_meals,
                        today_totals=today_totals,
                        plan=plan,
                        activity=activity,
                        had_workout=had_workout,
                        weight_today=weight,
                    )
                    await app.bot.send_message(
                        chat_id=uid,
                        text=f"🌙 *Итог дня*\n\n{summary}",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Daily summary {uid}: {e}")

        job_queue.run_daily(
            daily_summary,
            time=dtime(hour=23, minute=30),
            name="daily_summary"
        )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", show_day_plan))
    app.add_handler(CommandHandler("week", show_week_plan))
    app.add_handler(CommandHandler("analytics", show_analytics))
    app.add_handler(CommandHandler("import", import_command))
    app.add_handler(CommandHandler("analyze", analyze_history_command))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
