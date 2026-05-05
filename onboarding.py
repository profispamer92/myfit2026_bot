"""
AI-driven onboarding.
Claude leads the conversation, collects all needed data, returns JSON plan.
No state machine — Claude decides what to ask and when it has enough info.
"""
import json
import logging
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, filters
)
from database import Database
from ai_handler import AIHandler

logger = logging.getLogger(__name__)

CHATTING = 1

SYSTEM_PROMPT = """Ты опытный фитнес-тренер и нутрициолог. Твоя задача — познакомиться с новым клиентом и составить персональный план питания и тренировок.

Веди естественный диалог на русском языке. Собери следующую информацию (можно в любом порядке):
- Пол
- Возраст  
- Вес (кг)
- Рост (см)
- Процент жира (если знает — отлично, если нет — скажи что оценишь сам по другим данным)
- Цель: сушка / набор / поддержание
- Уровень активности вне тренировок (сидячий / лёгкий / умеренный / высокий)
- Сколько раз в неделю тренируется и в какие дни
- Примерное время тренировок

ПРАВИЛА ДИАЛОГА:
- Не задавай все вопросы сразу — 1-2 за раз, живой разговор
- Если написал несколько данных сразу — принимай все
- Если не знает процент жира — не настаивай, оцени по возрасту/полу/весу/росту приближённо
- Можешь делать промежуточные комментарии ("хороший результат для твоего возраста" и т.д.)
- Когда собрал всё — предложи план и жди подтверждения

РАСЧЁТ КБЖУ:
- Если есть процент жира → Katch-McArdle: BMR = 370 + 21.6 × (вес × (1 - жир/100))
- Если нет → Миффлин-Сан Жеор: мужчины 10×вес + 6.25×рост - 5×возраст + 5, женщины -161
- Коэффициент активности: сидячий 1.2, лёгкий 1.375, умеренный 1.55, высокий 1.725
- Сушка: TDEE × 0.8, белок 2.2г/кг lean mass (или от веса если нет % жира)
- Набор: TDEE × 1.1, белок 2.0г/кг
- Поддержание: TDEE × 1.0, белок 1.8г/кг
- Жиры: 25% от калорий, углеводы — остаток

ПЛАН ТРЕНИРОВОК — предложи сплит исходя из кол-ва дней:
- 2 дня: верх/низ
- 3 дня: грудь+трицепс / спина+бицепс / ноги+плечи
- 4 дня: грудь+трицепс / спина+бицепс / ноги / плечи+руки
- 5+ дней: по группам мышц

Когда пользователь подтвердил план (сказал "да", "ок", "сохрани", "подходит", "давай") —
верни ТОЛЬКО эту строку и ничего больше после неё:
PLAN_JSON:{"calories":2200,"protein":180,"fat":65,"carbs":220,"schedule":{"monday":{"name":"Грудь и трицепс","time":"18:00"}},"profile":{"weight":82,"height":180,"age":28,"gender":"male","fat_percent":18,"goal":"cut","activity":"moderate"},"summary":"краткое резюме на 1 предложение"}"""


def build_onboarding_handler(db: Database, ai: AIHandler):

    async def ob_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['ob_history'] = []
        await update.message.reply_text(
            "Привет! Давай составим твой персональный план 💪\n\n"
            "Расскажи о себе — начнём с главного: какая цель сейчас?\n"
            "_Сушка, набор массы или поддержание формы?_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return CHATTING

    async def ob_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text
        history = context.user_data.get('ob_history', [])

        history.append({"role": "user", "content": text})
        await update.message.chat.send_action("typing")

        try:
            response = ai.client.messages.create(
                model=ai.model,
                max_tokens=900,
                system=SYSTEM_PROMPT,
                messages=history
            )
            reply = response.content[0].text.strip()
        except Exception as e:
            logger.error(f"Onboarding AI error: {e}")
            await update.message.reply_text(
                "Что-то пошло не так, попробуй ещё раз."
            )
            return CHATTING

        history.append({"role": "assistant", "content": reply})
        context.user_data['ob_history'] = history

        if "PLAN_JSON:" in reply:
            return await _save_plan(update, context, reply, user_id)

        await update.message.reply_text(reply)
        return CHATTING

    async def _save_plan(update, context, reply, user_id):
        try:
            json_str = reply.split("PLAN_JSON:")[1].strip().split("\n")[0].strip()
            plan_data = json.loads(json_str)
        except Exception as e:
            logger.error(f"Plan JSON parse error: {e}")
            await update.message.reply_text(
                "Не смог сохранить план автоматически. Напиши 'сохрани план' ещё раз."
            )
            return CHATTING

        db.ensure_user(user_id)

        nutrition = {k: plan_data[k] for k in ('calories','protein','fat','carbs')}
        db.save_nutrition_plan(user_id, nutrition, is_base=True)

        if plan_data.get('schedule'):
            db.save_workout_schedule(user_id, plan_data['schedule'])

        profile = plan_data.get('profile', {})
        if profile:
            db.save_user_profile(user_id, profile)
            if profile.get('weight'):
                db.log_weight(user_id, profile['weight'])

        schedule = plan_data.get('schedule', {})
        days_ru = {
            "monday":"Пн","tuesday":"Вт","wednesday":"Ср",
            "thursday":"Чт","friday":"Пт","saturday":"Сб","sunday":"Вс"
        }
        sched_lines = [
            f"  {days_ru.get(d,d)}: {info['name']} в {info.get('time','?')}"
            for d, info in schedule.items()
        ]

        fat_str = f" | Жир: {profile.get('fat_percent')}%" if profile.get('fat_percent') else ""
        summary = plan_data.get('summary', '')

        from bot import main_keyboard
        await update.message.reply_text(
            f"✅ *План сохранён!*\n\n"
            f"👤 {profile.get('weight')}кг · {profile.get('height')}см · "
            f"{profile.get('age')}лет{fat_str}\n\n"
            f"🔥 {nutrition['calories']} ккал\n"
            f"🥩 Белок: {nutrition['protein']}г · "
            f"🧈 Жиры: {nutrition['fat']}г · "
            f"🍞 Углеводы: {nutrition['carbs']}г\n\n"
            f"💪 Тренировки:\n" + "\n".join(sched_lines) + "\n\n"
            f"_{summary}_\n\n"
            f"Готово! Кидай фото еды, пиши голосовым или текстом 💬",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

        context.user_data.pop('ob_history', None)
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[
            CommandHandler("setup", ob_start),
            MessageHandler(filters.Regex("^🚀 Настроить план$"), ob_start),
        ],
        states={
            CHATTING: [MessageHandler(filters.TEXT & ~filters.COMMAND, ob_chat)],
        },
        fallbacks=[CommandHandler("start", ob_start)],
        allow_reentry=True,
    )
