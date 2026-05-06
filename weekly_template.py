"""
Weekly template builder.
AI-driven conversation to create full weekly plan:
- Meals by day with time and KBJU
- Workouts
- Supplements
- Tasks
"""
import json
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
)
from database import Database
from ai_handler import AIHandler

logger = logging.getLogger(__name__)

CHATTING = 1

DAYS_RU = {
    "monday": "Понедельник", "tuesday": "Вторник", "wednesday": "Среда",
    "thursday": "Четверг", "friday": "Пятница", "saturday": "Суббота", "sunday": "Воскресенье"
}

SYSTEM_PROMPT = """Ты персональный фитнес-тренер. Помоги пользователю составить недельный шаблон плана.

Шаблон включает для каждого дня недели:
- Приёмы пищи: время, название, КБЖУ (калории, белок, жиры, углеводы)
- Тренировка (если есть): время, тип
- Таблетки/БАДы: название, доза, время
- Задачи (опционально): название, время

ВАЖНО:
- Тренировочные дни и дни отдыха имеют разное питание
- В тренировочный день: больше углеводов, приём пищи за 1.5ч до тренировки и через 1ч после
- Общий КБЖУ на день должен соответствовать плану пользователя
- Веди диалог естественно, уточняй детали

Когда пользователь подтвердил весь шаблон — верни ТОЛЬКО JSON в таком формате:
TEMPLATE_JSON:{
  "monday": {
    "type": "workout",
    "meals": [
      {"time": "08:00", "name": "Завтрак", "calories": 500, "protein": 40, "fat": 15, "carbs": 55},
      {"time": "12:30", "name": "Обед до тренировки", "calories": 600, "protein": 45, "fat": 20, "carbs": 70},
      {"time": "16:00", "name": "Протеин после тренировки", "calories": 200, "protein": 35, "fat": 3, "carbs": 10},
      {"time": "19:00", "name": "Ужин", "calories": 500, "protein": 40, "fat": 18, "carbs": 45}
    ],
    "workout": {"time": "14:30", "name": "Грудь и трицепс"},
    "supplements": [
      {"time": "08:00", "name": "Омега-3", "dose": "1г", "timing": "after_meal"},
      {"time": "14:00", "name": "Предтрен", "dose": "1 порция", "timing": "before_meal"}
    ],
    "tasks": []
  },
  "tuesday": {
    "type": "rest",
    "meals": [...],
    "workout": null,
    "supplements": [...],
    "tasks": []
  }
}

Не возвращай JSON пока пользователь явно не подтвердил ("да", "ок", "сохрани", "подходит")."""


def build_weekly_template_handler(db: Database, ai: AIHandler):

    async def wt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        profile = db.get_user_profile(user_id)
        plan = db.get_nutrition_plan(user_id)
        schedule = db.get_workout_schedule(user_id)
        supps = db.get_supplements(user_id)

        # Build context for AI
        context_parts = []
        if plan:
            context_parts.append(
                f"КБЖУ план: {plan['calories']} ккал | "
                f"Б:{plan['protein']}г Ж:{plan['fat']}г У:{plan['carbs']}г"
            )
        if schedule:
            days_str = ", ".join([
                f"{DAYS_RU.get(d, d)}: {i['name']} в {i.get('time','?')}"
                for d, i in schedule.items()
            ])
            context_parts.append(f"Тренировки: {days_str}")
        if supps:
            supps_str = ", ".join([f"{s['name']} {s['dose']}" for s in supps])
            context_parts.append(f"Таблетки/БАДы: {supps_str}")

        context.user_data['wt_history'] = []
        context.user_data['wt_context'] = "\n".join(context_parts)

        await update.message.reply_text(
            "📅 Составляем недельный шаблон!\n\n"
            "Я знаю твой план питания и расписание тренировок. "
            "Давай пройдёмся по каждому дню — начнём с понедельника.\n\n"
            "Сколько приёмов пищи обычно делаешь в тренировочный день и в день отдыха?",
            reply_markup=ReplyKeyboardRemove()
        )
        return CHATTING

    async def wt_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text
        history = context.user_data.get('wt_history', [])
        user_context = context.user_data.get('wt_context', '')

        history.append({"role": "user", "content": text})
        await update.message.chat.send_action("typing")

        system = SYSTEM_PROMPT
        if user_context:
            system = f"Данные пользователя:\n{user_context}\n\n" + system

        # Add current date
        now = datetime.now()
        days_ru = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
        system = f"Сегодня {days_ru[now.weekday()]}, {now.strftime('%d.%m.%Y')}.\n\n" + system

        try:
            response = ai.client.messages.create(
                model=ai.model,
                max_tokens=2000,
                system=system,
                messages=history
            )
            reply = response.content[0].text.strip()
        except Exception as e:
            logger.error(f"Weekly template AI error: {e}")
            await update.message.reply_text("Ошибка, попробуй ещё раз.")
            return CHATTING

        history.append({"role": "assistant", "content": reply})
        context.user_data['wt_history'] = history

        if "TEMPLATE_JSON:" in reply:
            return await _save_template(update, context, reply, user_id)

        await update.message.reply_text(reply)
        return CHATTING

    async def _save_template(update, context, reply, user_id):
        try:
            json_str = reply.split("TEMPLATE_JSON:")[1].strip()
            # Handle multiline JSON
            template = json.loads(json_str)
        except Exception as e:
            logger.error(f"Template JSON parse error: {e}")
            await update.message.reply_text(
                "Не смог сохранить шаблон. Напиши 'сохрани шаблон' ещё раз."
            )
            return CHATTING

        db.save_weekly_template(user_id, template)

        # Also update supplement schedule from template
        for day, day_plan in template.items():
            for supp in day_plan.get('supplements', []):
                # Save unique supplements
                existing = db.get_supplements(user_id)
                names = [s['name'].lower() for s in existing]
                if supp['name'].lower() not in names:
                    db.save_supplement(
                        user_id,
                        name=supp['name'],
                        dose=supp.get('dose', ''),
                        timing=supp.get('timing', 'independent'),
                        time_of_day=supp.get('time')
                    )
            break  # Only add supplements once

        # Build summary
        days_summary = []
        for day, day_plan in template.items():
            day_ru = DAYS_RU.get(day, day)
            meals = day_plan.get('meals', [])
            total_cal = sum(m.get('calories', 0) for m in meals)
            wtype = "тренировка" if day_plan.get('workout') else "отдых"
            days_summary.append(f"{day_ru}: {total_cal} ккал ({wtype})")

        from bot import main_keyboard
        await update.message.reply_text(
            "✅ *Недельный шаблон сохранён!*\n\n" +
            "\n".join(days_summary) + "\n\n"
            "Каждое утро в 6:00 я буду присылать план дня и спрашивать что меняем.\n"
            "Можешь и с вечера написать что завтра изменится — сразу скорректирую.",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

        context.user_data.pop('wt_history', None)
        context.user_data.pop('wt_context', None)
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[
            CommandHandler("week_plan", wt_start),
            MessageHandler(filters.Regex("^📅 Составить шаблон$"), wt_start),
        ],
        states={
            CHATTING: [MessageHandler(filters.TEXT & ~filters.COMMAND, wt_chat)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
