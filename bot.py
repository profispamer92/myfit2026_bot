import logging
import os
import asyncio
import tempfile
from datetime import time as dtime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from database import Database
from ai_handler import AIHandler
from onboarding import build_onboarding_handler
from weekly_template import build_weekly_template_handler
from monitor import ProactiveMonitor
from webserver import start_web_server
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

# ConversationHandler states
SETTING_PLAN = 1
SETTING_WORKOUT = 2


def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 План дня"), KeyboardButton("📅 План недели")],
        [KeyboardButton("📈 Аналитика")],
    ], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    db.ensure_user(user_id)

    plan = db.get_nutrition_plan(user_id)
    if not plan:
        await update.message.reply_text(
            f"Привет, {name}! 👋\n\n"
            "Я твой персональный фитнес-тренер в Telegram.\n\n"
            "📸 Принимаю скрины из FatSecret и фото еды\n"
            "💬 Понимаю свободный текст\n"
            "🤖 Слежу за прогрессом и пишу сам если вижу что-то важное\n\n"
            "Давай настроим твой план — займёт 2 минуты!",
            reply_markup=ReplyKeyboardMarkup([["🚀 Настроить план"]], resize_keyboard=True, one_time_keyboard=True)
        )
    else:
        await update.message.reply_text(
            f"С возвращением, {name}! 💪",
            reply_markup=main_keyboard()
        )


async def set_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Настройка плана питания\n\n"
        "Напиши свои цели в свободном формате, например:\n\n"
        "_Калории: 2500, белок 180г, жиры 80г, углеводы 250г_\n\n"
        "Или просто:\n"
        "_2500 калорий, 180 белка, 80 жиров, 250 углеводов_\n\n"
        "ИИ сам разберёт 🤖",
        parse_mode="Markdown"
    )
    return SETTING_PLAN


async def save_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    await update.message.reply_text("⏳ Обрабатываю...")

    plan = await ai.parse_nutrition_plan(text)
    if plan:
        db.save_nutrition_plan(user_id, plan)
        await update.message.reply_text(
            f"✅ План сохранён!\n\n"
            f"🔥 Калории: {plan['calories']} ккал\n"
            f"🥩 Белок: {plan['protein']}г\n"
            f"🧈 Жиры: {plan['fat']}г\n"
            f"🍞 Углеводы: {plan['carbs']}г\n\n"
            "Теперь настрой расписание тренировок: /setworkout",
            reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            "Не смог распознать план. Попробуй написать например:\n"
            "_2500 калорий, белок 180г, жиры 80г, углеводы 250г_",
            parse_mode="Markdown"
        )
    return ConversationHandler.END


async def set_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💪 Настройка расписания тренировок\n\n"
        "Опиши свой план в свободном формате, например:\n\n"
        "_Понедельник — грудь и трицепс\n"
        "Среда — спина и бицепс\n"
        "Пятница — ноги и плечи\n"
        "Обычно тренируюсь в 18:00, но могу сдвигать_\n\n"
        "Напиши своё расписание:",
        parse_mode="Markdown"
    )
    return SETTING_WORKOUT


async def save_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    await update.message.reply_text("⏳ Обрабатываю...")

    schedule = await ai.parse_workout_schedule(text)
    if schedule:
        db.save_workout_schedule(user_id, schedule)
        days_ru = {
            "monday": "Понедельник", "tuesday": "Вторник", "wednesday": "Среда",
            "thursday": "Четверг", "friday": "Пятница", "saturday": "Суббота", "sunday": "Воскресенье"
        }
        lines = []
        for day, info in schedule.items():
            lines.append(f"{days_ru.get(day, day)}: {info['name']} в {info.get('time', '?')}")
        await update.message.reply_text(
            "✅ Расписание сохранено!\n\n" + "\n".join(lines) + "\n\n"
            "Теперь можешь кидать скрины из FatSecret или писать что съел/потренировался!",
            reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text("Не смог распознать расписание. Попробуй ещё раз.")
    return ConversationHandler.END


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all food photos: FatSecret screenshots, nutrition labels, food photos, InBody"""
    user_id = update.effective_user.id

    # InBody photo mode
    if context.user_data.get('waiting_inbody_photo'):
        context.user_data.pop('waiting_inbody_photo', None)
        await handle_inbody_photo(update, context)
        return

    await update.message.reply_text("📸 Анализирую...")

    # Auto-detect InBody before food analysis
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = bytes(await file.download_as_bytearray())
    inbody_check = await ai.analyze_inbody_photo(photo_bytes)
    if inbody_check and inbody_check.get('found'):
        await _process_inbody(update, context, inbody_check)
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()

    today = db.get_today_totals(user_id)
    plan = db.get_nutrition_plan(user_id)

    result = await ai.analyze_food_photo(bytes(photo_bytes), today_totals=today, plan=plan)

    if not result:
        await update.message.reply_text(
            "Не смог распознать фото 🤔 Попробуй написать текстом что это.",
            reply_markup=main_keyboard()
        )
        return

    photo_type = result.get('photo_type', 'food')

    if photo_type == 'label':
        # Product label — show analysis, ask if they want to log it
        response = f"🏷 *{result.get('description', 'Продукт')}*"
        if result.get('serving_size'):
            response += f" ({result['serving_size']})\n\n"
        else:
            response += "\n\n"
        response += f"🔥 {result['calories']} ккал | Б: {result['protein']}г | Ж: {result['fat']}г | У: {result['carbs']}г\n"

        if result.get('fit_analysis'):
            response += f"\n🤖 *Анализ:*\n{result['fit_analysis']}\n"

        response += "\n_Записать этот продукт? Напиши «да» или укажи количество, например «150г»_"

        # Store pending meal in context for confirmation
        context.user_data['pending_meal'] = result
        await update.message.reply_text(response, parse_mode="Markdown", reply_markup=main_keyboard())

    else:
        # FatSecret screenshot or food photo — log immediately
        db.log_meal(user_id, result)
        today = db.get_today_totals(user_id)

        emoji = "📱" if photo_type == 'fatsecret' else "🍽"
        response = f"{emoji} *Записано: {result.get('description', 'Приём пищи')}*\n"
        response += f"🔥 {result['calories']} ккал | Б: {result['protein']}г | Ж: {result['fat']}г | У: {result['carbs']}г\n"

        if result.get('comment'):
            response += f"\n💬 {result['comment']}\n"

        if plan and today:
            cal_left = plan['calories'] - today['calories']
            prot_left = plan['protein'] - today['protein']
            fat_left = plan['fat'] - today['fat']
            carbs_left = plan['carbs'] - today['carbs']
            response += f"\n📊 *Осталось сегодня:*\n"
            response += f"🔥 {cal_left} ккал | Б: {prot_left}г | Ж: {fat_left}г | У: {carbs_left}г"

        await update.message.reply_text(response, parse_mode="Markdown", reply_markup=main_keyboard())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart text handler — figures out if it's food, workout, weight, or a question"""
    import re
    user_id = update.effective_user.id
    text = update.message.text

    # Maintain conversation history (last 10 exchanges)
    if 'chat_history' not in context.user_data:
        context.user_data['chat_history'] = []
    context.user_data['chat_history'].append({"role": "user", "content": text})
    if len(context.user_data['chat_history']) > 20:
        context.user_data['chat_history'] = context.user_data['chat_history'][-20:]

    # Handle InBody confirmation
    if 'pending_inbody_plan' in context.user_data:
        pending_plan = context.user_data.get('pending_inbody_plan')
        if "✅" in text or "Да" in text:
            context.user_data.pop('pending_inbody_plan', None)
            db.save_nutrition_plan(user_id, pending_plan, is_base=True)
            await update.message.reply_text(
                f"✅ *План обновлён по данным InBody!*\n\n"
                f"🔥 {pending_plan['calories']} ккал\n"
                f"🥩 Белок: {pending_plan['protein']}г | 🧈 Жиры: {pending_plan['fat']}г | 🍞 Углеводы: {pending_plan['carbs']}г",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )
            return
        elif "❌" in text or "Оставить" in text:
            context.user_data.pop('pending_inbody_plan', None)
            await update.message.reply_text("Оставили текущий план.", reply_markup=main_keyboard())
            return

    # Handle InBody mode buttons
    if context.user_data.get('inbody_mode'):
        context.user_data.pop('inbody_mode', None)
        if "Сфотографировать" in text:
            context.user_data['waiting_inbody_photo'] = True
            await update.message.reply_text(
                "📸 Отправь фото отчёта InBody — сделай чёткий снимок всего листа.",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        elif "История" in text:
            history = db.get_inbody_history(user_id)
            if not history:
                await update.message.reply_text("Нет сохранённых измерений InBody.", reply_markup=main_keyboard())
                return
            lines = ["📈 *История InBody:*\n"]
            for h in history:
                lines.append(
                    f"📅 {h['date']}: {h.get('weight','?')}кг | "
                    f"💪 {h.get('muscle_mass','?')}кг | "
                    f"🧈 {h.get('fat_percent','?')}%"
                )
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard())
            return
        elif "Вручную" in text:
            context.user_data['inbody_manual'] = {}
            await update.message.reply_text(
                "Введи данные из отчёта InBody.\n\n"
                "Напиши в формате (всё что знаешь, остальное пропусти):\n\n"
                "_Вес: 85\nМышцы: 38\nЖир: 20%\nBMR: 1820_",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
            return

    # Handle manual InBody input
    if context.user_data.get('inbody_manual') is not None:
        import re
        data = {}
        if m := re.search(r'вес[:\s]+(\d+\.?\d*)', text, re.I): data['weight'] = float(m.group(1))
        if m := re.search(r'мышц[^\d]*(\d+\.?\d*)', text, re.I): data['muscle_mass'] = float(m.group(1))
        if m := re.search(r'жир[^\d]*(\d+\.?\d*)%', text, re.I): data['fat_percent'] = float(m.group(1))
        if m := re.search(r'жир[^\d]*(\d+\.?\d*)\s*кг', text, re.I): data['fat_mass'] = float(m.group(1))
        if m := re.search(r'bmr[:\s]+(\d+)', text, re.I): data['bmr'] = int(m.group(1))

        if not data:
            await update.message.reply_text(
                "Не смог распознать данные. Попробуй написать например:\n" +
"\n" +
                "_Вес: 85, мышцы: 38, жир: 22%, BMR: 1820_",
                parse_mode="Markdown"
            )
            return

        context.user_data.pop('inbody_manual', None)
        data['found'] = True
        await _process_inbody(update, context, data)
        return

    # Handle analytics period selection
    if context.user_data.get('waiting_analytics'):
        period_map = {"📅 За неделю": "week", "🗓 За месяц": "month"}
        period = period_map.get(text)
        if period:
            context.user_data.pop('waiting_analytics')
            await update.message.reply_text("⏳ Собираю данные и строю графики...")
            plan = db.get_nutrition_plan(user_id)
            week_data = db.get_week_stats(user_id)
            month_data = db.get_month_stats(user_id)
            days = 30 if period == 'month' else 7
            weight_history = db.get_weight_history(user_id, days=days)
            activity_data = db.get_activity_history(user_id, days=days)

            if not plan:
                await update.message.reply_text("Сначала настрой план: /setup", reply_markup=main_keyboard())
                return

            # Text analysis
            analysis = await ai.generate_text_analytics(
                period=period,
                week_data=week_data,
                month_data=month_data,
                plan=plan,
                weight_history=weight_history,
                activity_data=activity_data,
            )
            label = "месяц" if period == "month" else "неделю"
            await update.message.reply_text(
                f"📈 *Аналитика за {label}*\n\n{analysis}",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )

            # Weight chart
            if weight_history:
                chart = generate_weight_chart(weight_history, label)
                if chart:
                    from telegram import InputFile
                    import io
                    await update.message.reply_photo(
                        photo=io.BytesIO(chart),
                        caption=f"📉 График веса за {label}"
                    )

            # KBJU chart
            data = month_data if period == 'month' else week_data
            if data:
                kbju_chart = generate_kbju_chart(data, plan)
                if kbju_chart:
                    import io
                    await update.message.reply_photo(
                        photo=io.BytesIO(kbju_chart),
                        caption="🍽 КБЖУ по дням"
                    )

            # Correlation chart
            if len(weight_history) >= 5 and data:
                corr_chart = generate_correlation_chart(weight_history, data, activity_data)
                if corr_chart:
                    import io
                    await update.message.reply_photo(
                        photo=io.BytesIO(corr_chart),
                        caption="🔗 Корреляции: калории и шаги → вес"
                    )
            return

    # Handle meal suggestion type selection
    if context.user_data.get('waiting_meal_type'):
        context.user_data.pop('waiting_meal_type')
        meal_map = {
            "🌅 Завтрак": "завтрак", "☀️ Обед": "обед",
            "🌙 Ужин": "ужин", "🍎 Перекус": "перекус"
        }
        meal_type = meal_map.get(text)
        if meal_type:
            await update.message.reply_text("⏳ Смотрю твою историю питания...")
            history = db.get_meal_history(user_id, days=30)
            today = db.get_today_totals(user_id)
            plan = db.get_nutrition_plan(user_id)
            suggestion = await ai.suggest_meal(
                meal_type=meal_type,
                today_totals=today,
                plan=plan,
                meal_history=history,
            )
            await update.message.reply_text(
                f"🥗 *Варианты на {meal_type}:*\n\n{suggestion}",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )
            return

    # Handle pending meal confirmation after label scan
    if 'pending_meal' in context.user_data:
        pending = context.user_data.pop('pending_meal')
        lower = text.lower().strip()
        confirmed = lower in ('да', 'yes', 'ок', 'ok', '+', 'записать', 'записи')
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*г', lower)
        amount_match = float(m.group(1).replace(',', '.')) if m else None

        if confirmed or amount_match:
            meal = dict(pending)
            if amount_match:
                scale = amount_match / 100.0
                meal['calories'] = round(pending['calories'] * scale)
                meal['protein'] = round(pending['protein'] * scale, 1)
                meal['fat'] = round(pending['fat'] * scale, 1)
                meal['carbs'] = round(pending['carbs'] * scale, 1)
                meal['description'] = f"{pending.get('description', 'Продукт')} {int(amount_match)}г"
            db.log_meal(user_id, meal)
            today_new = db.get_today_totals(user_id)
            plan = db.get_nutrition_plan(user_id)
            response = f"✅ Записано: {meal.get('description', 'продукт')}\n"
            response += f"🔥 {meal['calories']} ккал | Б: {meal['protein']}г | Ж: {meal['fat']}г | У: {meal['carbs']}г"
            if plan and today_new:
                cal_left = plan['calories'] - today_new['calories']
                response += f"\n\nОсталось: {cal_left} ккал"
            await update.message.reply_text(response, reply_markup=main_keyboard())
            return
        # User said something else — fall through to normal handling

    # Handle button presses
    if text == "📋 План дня":
        await show_day_plan_text(update, context)
        return
    elif text == "📅 План недели":
        await show_week_plan(update, context)
        return
    elif text == "🥗 Что поесть?":
        await update.message.reply_text(
            "Для какого приёма пищи?",
            reply_markup=ReplyKeyboardMarkup([
                ["🌅 Завтрак", "☀️ Обед"],
                ["🌙 Ужин", "🍎 Перекус"],
            ], resize_keyboard=True, one_time_keyboard=True)
        )
        context.user_data['waiting_meal_type'] = True
        return

    elif text == "📈 Аналитика":
        await update.message.reply_text(
            "За какой период?",
            reply_markup=ReplyKeyboardMarkup([
                ["📅 За неделю", "🗓 За месяц"],
            ], resize_keyboard=True, one_time_keyboard=True)
        )
        context.user_data['waiting_analytics'] = True
        return

    elif text == "🤖 Спросить ИИ":
        await update.message.reply_text(
            "Напиши свой вопрос — отвечу с учётом твоей истории питания и тренировок 💬"
        )
        return
    elif text == "⚖️ Записать вес":
        await update.message.reply_text("Напиши свой вес, например: _82.5 кг_ или просто _82.5_", parse_mode="Markdown")
        return

    await update.message.reply_text("⏳ Думаю...")

    # Get user context for AI
    today = db.get_today_totals(user_id)
    plan = db.get_nutrition_plan(user_id)
    schedule = db.get_workout_schedule(user_id)
    recent_logs = db.get_recent_logs(user_id, days=3)

    chat_history = context.user_data.get('chat_history', [])
    intent = await ai.classify_and_handle(
        text=text,
        today_totals=today,
        plan=plan,
        schedule=schedule,
        recent_logs=recent_logs,
        chat_history=chat_history[:-1]  # exclude current message already in history
    )

    if intent['type'] == 'meal':
        db.log_meal(user_id, intent['data'])
        today = db.get_today_totals(user_id)
        cal_left = (plan['calories'] - today['calories']) if plan else None
        response = f"✅ Записано: {intent['data'].get('description', 'приём пищи')}\n"
        response += f"🔥 {intent['data']['calories']} ккал | Б: {intent['data']['protein']}г | Ж: {intent['data']['fat']}г | У: {intent['data']['carbs']}г\n"
        if cal_left is not None:
            response += f"\nОсталось калорий сегодня: {cal_left} ккал"
        await update.message.reply_text(response, reply_markup=main_keyboard())

    elif intent['type'] == 'workout':
        db.log_workout(user_id, intent['data'])
        response = f"💪 Тренировка записана!\n\n{intent['data'].get('summary', '')}"
        await update.message.reply_text(response, reply_markup=main_keyboard())

    elif intent['type'] == 'weight':
        db.log_weight(user_id, intent['data']['weight'])
        await update.message.reply_text(
            f"⚖️ Вес записан: {intent['data']['weight']} кг",
            reply_markup=main_keyboard()
        )

    elif intent['type'] in ('activity', 'health_sync'):
        from datetime import date, timedelta
        data = intent['data']
        sync_type = data.get('sync_type', 'evening')
        is_morning = sync_type == 'morning'

        response_parts = [
            f"{'🌅' if is_morning else '🌙'} *{'Утренняя' if is_morning else 'Вечерняя'} сводка из Apple Health*\n"
        ]

        # Weight — morning only
        weight = data.get('weight')
        if weight:
            measured_at = data.get('weight_measured_at', '')
            weight_date = date.today().isoformat()
            db.log_weight_for_date(user_id, weight, weight_date, measured_at)
            history = db.get_weight_history(user_id, days=8)
            prev = next((w['weight'] for w in history[1:] if w['weight'] != weight), None)
            diff_str = ""
            if prev:
                diff = round(weight - prev, 1)
                sign = "+" if diff > 0 else ""
                diff_str = f" ({sign}{diff} кг)"
            time_str = f" (взвешивался в {measured_at})" if measured_at else ""
            response_parts.append(f"⚖️ Вес: *{weight} кг*{diff_str}{time_str}")

        # Steps — morning = yesterday's final, evening = today's current
        steps = data.get('steps', 0)
        burned = data.get('calories_burned', 0)
        if steps and not burned:
            burned = round(steps * 0.04)

        if steps:
            steps_date = date.today().isoformat()
            label = "Шаги за сегодня"
            if is_morning or data.get('steps_date') == 'yesterday':
                steps_date = (date.today() - timedelta(days=1)).isoformat()
                label = "Шаги за вчера (итог)"
            db.log_activity_for_date(user_id, {
                'steps': steps,
                'calories_burned': burned,
                'source': 'shortcuts'
            }, steps_date)
            response_parts.append(f"👟 {label}: {steps:,}")
            if burned:
                response_parts.append(f"🔥 Сожжено: {burned} ккал")

        # Workouts — evening only typically
        workouts = data.get('workouts', [])
        if workouts:
            response_parts.append("")
            for w in workouts:
                wtype = w.get('type', 'Тренировка')
                dur = w.get('duration_min', 0)
                wcal = w.get('calories', 0)
                response_parts.append(f"💪 {wtype}: {dur} мин, ~{wcal} ккал")
                db.log_workout(user_id, {
                    'type': wtype,
                    'summary': f"{wtype} {dur} мин (Apple Health)",
                    'exercises': []
                })

        # Calorie summary — evening only (morning is start of day)
        if not is_morning:
            total_burned = burned + sum(w.get('calories', 0) for w in workouts)
            if total_burned > 0 and plan:
                effective = plan['calories'] + total_burned
                today_cal = (today or {}).get('calories', 0)
                cal_left = effective - today_cal
                response_parts.append(f"\n📊 С учётом активности лимит: *{effective}* ккал")
                response_parts.append(f"Съедено: {today_cal} → осталось: *{cal_left}* ккал")
        else:
            # Morning — show yesterday's summary briefly
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            y_data = db.get_week_stats(user_id)
            y_entry = next((d for d in y_data if d['date'] == yesterday), None)
            if y_entry and plan:
                diff = y_entry['calories'] - plan['calories']
                sign = "+" if diff > 0 else ""
                response_parts.append(f"\n📊 Вчера: {y_entry['calories']} ккал ({sign}{diff} от плана)")

        await update.message.reply_text(
            "\n".join(response_parts),
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif intent['type'] == 'workout_reschedule':
        # User is moving workout time — recalculate meal timing
        new_time = intent['data']['new_time']
        day = intent['data'].get('day', 'сегодня')
        meal_plan = await ai.recalculate_meal_timing(
            nutrition_plan=plan,
            workout_time=new_time,
            today_logs=recent_logs
        )
        response = f"🔄 Тренировка сдвинута на {new_time}\n\n"
        response += f"📋 Рекомендую скорректировать питание:\n{meal_plan}"
        await update.message.reply_text(response, reply_markup=main_keyboard())

    elif intent['type'] == 'add_supplement':
        data = intent['data']
        db.save_supplement(
            user_id,
            name=data.get('name', ''),
            dose=data.get('dose', ''),
            timing=data.get('timing', 'independent'),
            time_of_day=data.get('time_of_day')
        )
        timing_ru = {'before_meal':'до еды','after_meal':'после еды','with_meal':'во время еды','independent':'независимо'}
        timing_str = timing_ru.get(data.get('timing',''), '')
        time_str = f" в {data['time_of_day']}" if data.get('time_of_day') else ""
        await update.message.reply_text(
            f"💊 Добавлено: *{data.get('name')}* {data.get('dose')} · {timing_str}{time_str}\n\n"
            f"Буду показывать в плане дня. Выпил — ставь галочку в приложении или напиши «выпил омегу».",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif intent['type'] == 'add_task':
        data = intent['data']
        db.save_task(
            user_id,
            title=data.get('title', ''),
            time_str=data.get('time_str'),
            repeat=data.get('repeat', 'none')
        )
        time_str = f" в {data['time_str']}" if data.get('time_str') else ""
        repeat_str = " (каждый день)" if data.get('repeat') == 'daily' else ""
        await update.message.reply_text(
            f"✅ Задача добавлена: *{data.get('title')}*{time_str}{repeat_str}",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif intent['type'] == 'product_fit':
        # "Can I eat X?" — dedicated handler
        answer = await ai.check_product_fit(
            product_text=text,
            today_totals=today,
            plan=plan
        )
        await update.message.reply_text(answer, reply_markup=main_keyboard())

    elif intent['type'] == 'plan_confirm':
        # User confirming a pending plan change
        pending = db.get_pending_plan(user_id)
        if pending and pending.get('new_plan'):
            db.save_nutrition_plan(user_id, pending['new_plan'], is_base=True)
            db.clear_pending_plan(user_id)
            p = pending['new_plan']
            await update.message.reply_text(
                f"✅ *Новый план сохранён!*\n\n"
                f"🔥 {p['calories']} ккал | Б:{p['protein']}г Ж:{p['fat']}г У:{p['carbs']}г",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )
        else:
            await update.message.reply_text("Нет ожидающих изменений плана.", reply_markup=main_keyboard())

    elif intent['type'] == 'today_override':
        # Temporary change for today only
        if intent.get('data'):
            db.save_daily_override(user_id, intent['data'], reason="user_request")
            p = intent['data']
            await update.message.reply_text(
                f"✅ *План на сегодня скорректирован*\n"
                f"🔥 {p['calories']} ккал | Б:{p['protein']}г Ж:{p['fat']}г У:{p['carbs']}г\n\n"
                f"_Завтра вернётся базовый план_",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )

    elif intent['type'] == 'multi_action':
        actions = intent.get('actions', [])
        results = []
        for action in actions:
            atype = action.get('type')
            adata = action.get('data', {})
            if atype == 'add_task':
                db.save_task(user_id, adata.get('title',''), adata.get('time_str'), adata.get('repeat','none'))
                results.append(f"✅ {adata.get('title','')}")
            elif atype == 'add_supplement':
                db.save_supplement(user_id, adata.get('name',''), adata.get('dose',''), adata.get('timing','independent'), adata.get('time_of_day'))
                results.append(f"💊 {adata.get('name','')} {adata.get('dose','')}")
            elif atype == 'meal':
                db.log_meal(user_id, adata)
                results.append(f"🍽 {adata.get('description','')} — {adata.get('calories',0)} ккал")
            elif atype == 'weight':
                db.log_weight(user_id, adata['weight'])
                results.append(f"⚖️ {adata['weight']} кг")
        response = "Готово!\n" + "\n".join(results)
        await update.message.reply_text(response, reply_markup=main_keyboard())

    elif intent['type'] == 'question':
        response = intent['answer']
        context.user_data.get('chat_history', []).append({"role": "assistant", "content": response})
        await update.message.reply_text(response, reply_markup=main_keyboard())

    else:
        await update.message.reply_text(
            "Не понял 🤔 Попробуй написать что съел, как потренировался, или задай вопрос.",
            reply_markup=main_keyboard()
        )


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = db.get_today_totals(user_id)
    plan = db.get_nutrition_plan(user_id)
    meals = db.get_today_meals(user_id)

    if not today or today['calories'] == 0:
        await update.message.reply_text("Сегодня ещё ничего не записано 📭", reply_markup=main_keyboard())
        return

    def bar(current, target, length=10):
        if not target:
            return "━" * length
        filled = min(int((current / target) * length), length)
        return "█" * filled + "░" * (length - filled)

    response = "📊 *Статистика за сегодня*\n\n"

    if plan:
        cal_pct = int((today['calories'] / plan['calories']) * 100) if plan['calories'] else 0
        response += f"🔥 Калории: {today['calories']} / {plan['calories']} ккал ({cal_pct}%)\n"
        response += f"`{bar(today['calories'], plan['calories'])}`\n\n"
        response += f"🥩 Белок:  {today['protein']}г / {plan['protein']}г\n"
        response += f"🧈 Жиры:   {today['fat']}г / {plan['fat']}г\n"
        response += f"🍞 Углеводы: {today['carbs']}г / {plan['carbs']}г\n"
    else:
        response += f"🔥 Калории: {today['calories']} ккал\n"
        response += f"🥩 Белок: {today['protein']}г | 🧈 Жиры: {today['fat']}г | 🍞 Углеводы: {today['carbs']}г\n"

    if meals:
        response += "\n📝 *Приёмы пищи:*\n"
        for meal in meals:
            response += f"• {meal['description']} — {meal['calories']} ккал\n"

    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=main_keyboard())


async def show_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    week_data = db.get_week_stats(user_id)
    plan = db.get_nutrition_plan(user_id)

    if not week_data:
        await update.message.reply_text("Нет данных за эту неделю 📭", reply_markup=main_keyboard())
        return

    response = "📅 *Статистика за неделю*\n\n"
    total_cal = 0
    days_count = 0

    for day in week_data:
        total_cal += day['calories']
        days_count += 1
        status = "✅" if plan and day['calories'] >= plan['calories'] * 0.9 else "⚠️"
        response += f"{status} {day['date']}: {day['calories']} ккал | Б:{day['protein']}г\n"

    if days_count:
        avg = total_cal // days_count
        response += f"\n📈 Среднее: {avg} ккал/день"
        if plan:
            diff = avg - plan['calories']
            sign = "+" if diff > 0 else ""
            response += f" ({sign}{diff} от плана)"

    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=main_keyboard())


async def show_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    plan = db.get_nutrition_plan(user_id)

    if not plan:
        await update.message.reply_text(
            "План не настроен. Используй /setplan",
            reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text(
        f"🍽 *Твой план питания:*\n\n"
        f"🔥 Калории: {plan['calories']} ккал\n"
        f"🥩 Белок: {plan['protein']}г\n"
        f"🧈 Жиры: {plan['fat']}г\n"
        f"🍞 Углеводы: {plan['carbs']}г\n\n"
        "Чтобы изменить — /setplan",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


async def show_workout_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    schedule = db.get_workout_schedule(user_id)

    if not schedule:
        await update.message.reply_text(
            "Расписание не настроено. Используй /setworkout",
            reply_markup=main_keyboard()
        )
        return

    days_ru = {
        "monday": "Пн", "tuesday": "Вт", "wednesday": "Ср",
        "thursday": "Чт", "friday": "Пт", "saturday": "Сб", "sunday": "Вс"
    }
    lines = []
    for day, info in schedule.items():
        lines.append(f"{days_ru.get(day, day)}: {info['name']} — {info.get('time', '?')}")

    await update.message.reply_text(
        "💪 *Расписание тренировок:*\n\n" + "\n".join(lines) + "\n\n"
        "Чтобы изменить — /setworkout\n"
        "Чтобы сдвинуть сегодняшнюю — просто напиши, например:\n"
        "_Сегодня трен в 20:00_",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )




async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF and other documents"""
    user_id = update.effective_user.id
    doc = update.message.document
    
    if not doc:
        return
    
    mime = doc.mime_type or ""
    
    # Only handle PDFs and images sent as files
    if "pdf" not in mime and "image" not in mime:
        await update.message.reply_text(
            "Пока умею читать только PDF и изображения. "
            "Для InBody скинь PDF или сфотографируй распечатку.",
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
            logger.error(f"PDF convert error: {e}")
            await update.message.reply_text(
                "Не смог прочитать PDF. Попробуй сделать скриншот и прислать как фото.",
                reply_markup=main_keyboard()
            )
            return
    else:
        img_bytes = file_bytes
    
    # Check if it's InBody
    if context.user_data.get('waiting_inbody_photo'):
        context.user_data.pop('waiting_inbody_photo', None)
        # Reuse InBody photo handler logic
        today = db.get_today_totals(user_id)
        plan = db.get_nutrition_plan(user_id)
        inbody = await ai.analyze_inbody_photo(img_bytes)
        if inbody:
            await _process_inbody(update, context, inbody)
        else:
            await update.message.reply_text(
                "Не смог прочитать InBody из файла. Попробуй сфотографировать распечатку.",
                reply_markup=main_keyboard()
            )
        return
    
    # Otherwise treat as food photo
    today = db.get_today_totals(user_id)
    plan = db.get_nutrition_plan(user_id)
    result = await ai.analyze_food_photo(img_bytes, today_totals=today, plan=plan)
    
    if not result:
        # Maybe it's InBody — try that
        inbody = await ai.analyze_inbody_photo(img_bytes)
        if inbody:
            await _process_inbody(update, context, inbody)
            return
        await update.message.reply_text(
            "Не смог распознать содержимое файла. Попробуй прислать как фото.",
            reply_markup=main_keyboard()
        )
        return

    photo_type = result.get('photo_type', 'food')
    if photo_type == 'label':
        response = f"🏷 *{result.get('description', 'Продукт')}*"
        if result.get('serving_size'):
            response += f" ({result['serving_size']})\n\n"
        else:
            response += "\n\n"
        response += f"🔥 {result['calories']} ккал | Б: {result['protein']}г | Ж: {result['fat']}г | У: {result['carbs']}г\n"
        if result.get('fit_analysis'):
            response += f"\n🤖 *Анализ:*\n{result['fit_analysis']}\n"
        response += "\n_Записать? Напиши «да» или укажи количество, например «150г»_"
        context.user_data['pending_meal'] = result
        await update.message.reply_text(response, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        db.log_meal(user_id, result)
        today = db.get_today_totals(user_id)
        response = f"📄 *Записано: {result.get('description', 'Приём пищи')}*\n"
        response += f"🔥 {result['calories']} ккал | Б: {result['protein']}г | Ж: {result['fat']}г | У: {result['carbs']}г\n"
        if result.get('comment'):
            response += f"\n💬 {result['comment']}\n"
        if plan and today:
            cal_left = plan['calories'] - today['calories']
            response += f"\n📊 Осталось: {cal_left} ккал"
        await update.message.reply_text(response, parse_mode="Markdown", reply_markup=main_keyboard())


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe voice message via Whisper then handle as text"""
    user_id = update.effective_user.id
    await update.message.reply_text("🎤 Слушаю...")

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        audio_bytes = await file.download_as_bytearray()

        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        # Transcribe via Whisper
        text = await ai.transcribe_voice(tmp_path)
        os.unlink(tmp_path)

        if not text:
            await update.message.reply_text(
                "Не смог разобрать голосовое 🤔 Попробуй ещё раз или напиши текстом.",
                reply_markup=main_keyboard()
            )
            return

        # Show what was recognized
        await update.message.reply_text(f"🎤 _Распознал: {text}_", parse_mode="Markdown")

        # Process as regular text message
        update.message.text = text
        await handle_text(update, context)

    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        await update.message.reply_text(
            "Ошибка при обработке голосового. Попробуй написать текстом.",
            reply_markup=main_keyboard()
        )



async def inbody_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start InBody flow — offer photo or manual input"""
    await update.message.reply_text(
        "📊 *Анализ InBody*"

"\n"
        "Как хочешь ввести данные?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([
            ["📸 Сфотографировать отчёт"],
            ["✏️ Ввести вручную"],
            ["📈 История InBody"],
        ], resize_keyboard=True, one_time_keyboard=True)
    )
    context.user_data['inbody_mode'] = True


async def handle_inbody_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process InBody report photo"""
    user_id = update.effective_user.id
    await update.message.reply_text("📊 Читаю отчёт InBody...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = bytes(await file.download_as_bytearray())

    inbody = await ai.analyze_inbody_photo(photo_bytes)
    if not inbody:
        await update.message.reply_text(
            "Не смог прочитать отчёт. Попробуй сфотографировать чётче или введи данные вручную: /inbody",
            reply_markup=main_keyboard()
        )
        return

    await _process_inbody(update, context, inbody)


async def _process_inbody(update: Update, context: ContextTypes.DEFAULT_TYPE, inbody: dict):
    """Common InBody processing after data is collected"""
    user_id = update.effective_user.id

    # Get full profile including goal
    profile = db.get_user_profile(user_id) or {}
    goal_type = profile.get('goal_type') or profile.get('goal') or 'cut'
    goal_value = profile.get('goal_value')

    # Calculate KBJU using all available data
    kbju = await ai.calculate_kbju_unified(inbody, profile, goal_type, goal_value)
    summary = await ai.format_inbody_summary(inbody, kbju, goal)

    # Save InBody data
    db.save_inbody(user_id, inbody)

    # Build response
    fat_pct = inbody.get('fat_percent', '?')
    muscle = inbody.get('muscle_mass', '?')
    fat_mass = inbody.get('fat_mass', '?')
    weight = inbody.get('weight', '?')
    bmr = kbju.get('bmr', '?')

    response = (
        f"📊 *Данные InBody*"

"\n"
        f"⚖️ Вес: {weight} кг\n"
"\n"
        f"💪 Мышечная масса: {muscle} кг\n"
"\n"
        f"🧈 Жировая масса: {fat_mass} кг ({fat_pct}%)\n"
"\n"
        f"🔥 Базовый метаболизм: {bmr} ккал"

"\n"
        f"🤖 {summary}"

"\n"
        f"*Рассчитанный план питания:*\n"
"\n"
        f"🔥 {kbju['calories']} ккал\n"
"\n"
        f"🥩 Белок: {kbju['protein']}г | 🧈 Жиры: {kbju['fat']}г | 🍞 Углеводы: {kbju['carbs']}г"

"\n"
        f"Применить этот план?"
    )

    context.user_data['pending_inbody_plan'] = kbju
    await update.message.reply_text(
        response,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([
            ["✅ Да, применить"],
            ["❌ Оставить текущий план"],
        ], resize_keyboard=True, one_time_keyboard=True)
    )




async def show_week_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show weekly template overview"""
    user_id = update.effective_user.id
    from datetime import datetime, timedelta, date
    template = db.get_weekly_template(user_id)

    days = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    days_ru = {
        "monday":"Пн","tuesday":"Вт","wednesday":"Ср",
        "thursday":"Чт","friday":"Пт","saturday":"Сб","sunday":"Вс"
    }

    if not template:
        await update.message.reply_text(
            "Недельный шаблон не составлен.\n"
            "Напиши /week_plan чтобы составить с ботом.",
            reply_markup=main_keyboard()
        )
        return

    # Show Mon-Sun with status
    today = date.today()
    today_day = days[datetime.now().weekday()]
    lines = ["📅 *План недели*\n"]

    for day in days:
        day_plan = template.get(day, {})
        meals = day_plan.get('meals', [])
        workout = day_plan.get('workout')
        total_cal = sum(m.get('calories', 0) for m in meals)

        # Find date for this day
        day_idx = days.index(day)
        today_idx = datetime.now().weekday()
        delta = (day_idx - today_idx) % 7
        day_date = today + timedelta(days=delta)

        # Check if override exists for this day
        override = db.get_day_override(user_id, day_date.isoformat())
        marker = "🔄 " if override else ""
        is_today = "👉 " if day == today_day else ""

        workout_str = f" | 💪 {workout['name']}" if workout else ""
        lines.append(
            f"{is_today}{marker}*{days_ru[day]}* {day_date.strftime('%d.%m')}: "
            f"{total_cal} ккал{workout_str}"
        )

        # Show meals for today and tomorrow
        if day == today_day or delta == 1:
            for m in meals:
                lines.append(f"  {m['time']} {m['name']} — {m['calories']} ккал")

    lines.append("\n_Чтобы пересоставить шаблон: /week_plan_")
    lines.append("_Чтобы изменить конкретный день — напиши мне_")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


async def show_day_plan_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show full day plan as text in chat"""
    user_id = update.effective_user.id
    d = db.get_full_day_plan(user_id)

    plan = d.get('plan') or {}
    totals = d.get('totals') or {}
    meals = d.get('meals', [])
    supps = d.get('supplements', [])
    taken = d.get('supplements_taken', [])
    tasks = d.get('tasks', [])
    done_ids = d.get('tasks_done', [])
    workout = d.get('workout')
    weight = d.get('weight')
    sleep_data = d.get('sleep')
    activity = d.get('activity') or {}

    lines = ["📋 *План дня*\n"]

    # Stats
    if weight:
        lines.append(f"⚖️ Вес: {weight} кг")
    if sleep_data:
        lines.append(f"💤 Сон: {sleep_data['hours']}ч")
    if activity.get('steps'):
        lines.append(f"👟 Шаги: {activity['steps']:,}")
    if weight or sleep_data or activity.get('steps'):
        lines.append("")

    # КБЖУ
    cal = totals.get('calories', 0)
    plan_cal = plan.get('calories', 0)
    if plan_cal:
        pct = int(cal / plan_cal * 100)
        bar_len = 12
        filled = min(bar_len, int(pct / 100 * bar_len))
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"🔥 *Калории:* {cal} / {plan_cal} ккал ({pct}%)")
        lines.append(f"`{bar}`")
        lines.append(f"🥩 Б: {totals.get('protein',0)}г / {plan.get('protein',0)}г  "
                     f"🧈 Ж: {totals.get('fat',0)}г / {plan.get('fat',0)}г  "
                     f"🍞 У: {totals.get('carbs',0)}г / {plan.get('carbs',0)}г")
        lines.append("")

    # Workout
    if workout:
        lines.append(f"💪 *Тренировка:* {workout['name']} в {workout.get('time','?')}")
        lines.append("")

    # Supplements
    if supps:
        lines.append("💊 *Таблетки и БАДы:*")
        for s in supps:
            tick = "✅" if s['id'] in taken else "⬜"
            time_str = f" {s['time_of_day']}" if s.get('time_of_day') else ""
            timing = f" · {s['timing']}" if s.get('timing') else ""
            lines.append(f"{tick} {s['name']} {s['dose']}{timing}{time_str}")
        lines.append("")

    # Meals
    if meals:
        lines.append("🍽 *Питание:*")
        for m in meals:
            lines.append(f"  {m['time']} {m['description']} — {m['calories']} ккал")
        lines.append("")

    # Tasks
    if tasks:
        lines.append("✅ *Задачи:*")
        for t in tasks:
            tick = "✅" if t['id'] in done_ids else "⬜"
            time_str = f" {t['time_str']}" if t.get('time_str') else ""
            lines.append(f"{tick} {t['title']}{time_str}")
        lines.append("")

    if not meals and not supps and not tasks and not workout:
        lines.append("_День пока пустой_")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handlers
    plan_conv = ConversationHandler(
        entry_points=[CommandHandler("setplan", set_plan),
                      MessageHandler(filters.Regex("^🍽 Мой план$"), set_plan)],
        states={SETTING_PLAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_plan)]},
        fallbacks=[]
    )

    workout_conv = ConversationHandler(
        entry_points=[CommandHandler("setworkout", set_workout),
                      MessageHandler(filters.Regex("^💪 Тренировки$"), set_workout)],
        states={SETTING_WORKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_workout)]},
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("inbody", inbody_command))
    app.add_handler(CommandHandler("plan", show_day_plan_text))
    app.add_handler(build_onboarding_handler(db, ai))
    app.add_handler(build_weekly_template_handler(db, ai))
    app.add_handler(CommandHandler('week_plan', lambda u, c: build_weekly_template_handler(db, ai)))
    app.add_handler(plan_conv)
    app.add_handler(workout_conv)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.Regex("^🚀 Настроить план$"), lambda u, c: __import__('onboarding').ob_start(u, c)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Proactive monitor — runs every day at 20:00
    monitor = ProactiveMonitor(db=db, ai=ai, bot=app.bot)
    job_queue = app.job_queue
    job_queue.run_daily(
        monitor.run_all_checks,
        time=dtime(hour=20, minute=0),
        name="daily_monitor"
    )

    # Morning checkin — every day at 6:00
    async def send_morning_checkins(context):
        from datetime import date, datetime
        today = date.today().isoformat()
        days = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
        days_ru = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
        today_ru = days_ru[datetime.now().weekday()]
        today_day = days[datetime.now().weekday()]

        users = db.get_all_users()
        for user in users:
            uid = user['user_id']
            try:
                if db.get_morning_checkin_done(uid, today):
                    continue
                template = db.get_weekly_template(uid)
                if not template:
                    continue
                day_plan = db.get_effective_day_plan(uid)
                if not day_plan:
                    continue

                meals = day_plan.get('meals', [])
                workout = day_plan.get('workout')
                supps = day_plan.get('supplements', [])
                tasks = day_plan.get('tasks', [])
                total_cal = sum(m.get('calories', 0) for m in meals)
                total_prot = sum(m.get('protein', 0) for m in meals)

                lines = [f"🌅 *Доброе утро! План на {today_ru}:*\n"]
                if workout:
                    lines.append(f"💪 Тренировка: {workout['name']} в {workout['time']}")
                lines.append(f"\n🍽 *Питание* ({total_cal} ккал, Б:{total_prot}г):")
                for m in meals:
                    lines.append(f"  {m['time']} {m['name']} — {m['calories']} ккал")
                if supps:
                    lines.append("\n💊 *Таблетки:*")
                    for s in supps:
                        lines.append(f"  {s['time']} {s['name']} {s['dose']}")
                if tasks:
                    lines.append("\n✅ *Задачи:*")
                    for t in tasks:
                        time_str = f" {t['time']}" if t.get('time') else ""
                        lines.append(f"  {t['name']}{time_str}")

                lines.append("\n_Что меняем на сегодня? Или напиши «всё по плану»_")

                await app.bot.send_message(
                    chat_id=uid,
                    text="\n".join(lines),
                    parse_mode="Markdown"
                )
                db.save_morning_checkin_done(uid, today)
            except Exception as e:
                logger.error(f"Morning checkin error for {uid}: {e}")

    job_queue.run_daily(
        send_morning_checkins,
        time=dtime(hour=6, minute=0),
        name="morning_checkin"
    )

    # Daily summary — every day at 23:30
    async def send_daily_summaries(context):
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
                logger.error(f"Daily summary error for {uid}: {e}")

    job_queue.run_daily(
        send_daily_summaries,
        time=dtime(hour=23, minute=30),
        name="daily_summary"
    )

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
