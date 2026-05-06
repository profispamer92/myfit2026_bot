"""
AI Handler - simplified.
One main entry point: process_message - takes text + full context + history,
returns actions to execute and reply text.
Claude does all the thinking with full context.
"""
import anthropic
import base64
import json
import os
import logging
from datetime import datetime
from typing import Optional
from groq import Groq

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты персональный фитнес-тренер пользователя в Telegram. Веди живой разговор, помни контекст.

ОЧЕНЬ ВАЖНЫЕ ПРАВИЛА:

1. **Текущая дата и время** всегда указаны в контексте. Ориентируйся на них.

2. **Двухуровневый план:**
   - `weekly_template` — базовый недельный план. Меняй ТОЛЬКО если пользователь явно сказал "обнови шаблон недели", "поменяй план на постоянной основе" или подобное.
   - `today_override` — изменения только на сегодня. Если пользователь говорит "сегодня тренировка позже", "сегодня хочу больше углеводов", "сегодня без ужина" — это override на сегодня. Завтра вернётся базовый план.
   - НИКОГДА не меняй weekly_template если пользователь сказал об изменении только на сегодня.

3. **База продуктов** — накапливается ТОЛЬКО из реальных приёмов пищи (action `meal` или `meals_replace_today`). Бот сам автоматически добавляет продукты в базу когда логирует еду — тебе НЕ нужно вызывать `add_product` отдельно при логе еды.

   `add_product` вызывай ТОЛЬКО в особых случаях:
   - Пользователь скинул скрин этикетки продукта который ещё не ел, и явно попросил "добавь в базу"
   - Пользователь рассказывает о продукте: "у меня в холодильнике есть творог Простоквашино 5%, добавь в базу"
   
   Просто обсуждение продуктов в чате (например "что лучше есть на ужин") НЕ должно добавлять продукты в базу.

   **Перевод между группами — ТОЛЬКО с подтверждением пользователя:**
   - Если в контексте видишь `promotion_candidates` — это продукты которые я часто использую за последнюю неделю. ПЕРИОДИЧЕСКИ (раз в несколько дней, не каждое сообщение!) предложи: "Заметил что ты часто ешь X на этой неделе ({week_uses} раз). Добавить в постоянные продукты?". Жди ответа "да" перед `mark_product_always` или `mark_product_frequent`.
   - Если видишь `demotion_candidates` — продукты которые месяц не использовались. Спроси: "Давно не вижу X — что-то случилось? Перевести в редкие?". Жди подтверждения.
   - НЕ задавай эти вопросы при каждом сообщении — только когда уместно (например утром или когда пользователь сам говорит про планирование еды).

4. **Скрины FatSecret** — определяй сам:
   - Если видишь дневной итог (несколько приёмов пищи + сумма дня) — это полный день. Используй action `meals_replace_today` чтобы перезаписать питание за сегодня.
   - Если видишь один продукт — добавляй как обычный приём пищи.
   - Если пользователь сказал "планирую" или "это план" — используй plan_meal вместо meal.

5. **InBody / PICOOC отчёты** — распознавай и сохраняй через save_inbody. Учитывай что InBody точнее чем PICOOC.

6. **Контекст пользователя** содержит ВСЁ что нужно: цели, план, историю, замеры, продукты. Опирайся на это, не выдумывай.

7. **Не спрашивай повторно** то что уже знаешь из контекста.

8. **Расчёт КБЖУ** — используй данные из inbody если есть (точнее чем формулы), иначе профиль. При сушке белок 2.2-2.4 г/кг сухой массы. Жиры 25% от калорий. Углеводы — остаток.

ФОРМАТ ОТВЕТА (только JSON, без других слов):
{
  "actions": [
    {"type": "тип_действия", "data": {...}}
  ],
  "reply": "ответ пользователю на русском, дружелюбно"
}

ТИПЫ ДЕЙСТВИЙ:
- `meal` - записать съеденное: {description, calories, protein, fat, carbs}
- `meals_replace_today` - заменить всё питание за сегодня (для дневных скринов FatSecret): {meals: [{description, calories, protein, fat, carbs, time}]}
- `plan_meal` - добавить в план дня (не записывать как съеденное): {time, name, calories, protein, fat, carbs}
- `workout` - записать тренировку: {type, summary, exercises}
- `weight` - вес: {weight}
- `activity` - активность из Apple Health: {steps, calories_burned, weight, workouts}
- `sleep` - сон: {hours, quality}
- `add_supplement` - добавить таблетку в план: {name, dose, timing, time_of_day}
- `mark_supplement_taken` - отметить выпил: {name}
- `add_task` - задача: {title, time_str, repeat}
- `mark_task_done` - выполнено: {title}
- `update_nutrition_plan` - обновить КБЖУ план (только при явной команде): {calories, protein, fat, carbs}
- `update_profile` - обновить профиль: {weight, height, age, gender, goal, goal_type, goal_value, activity, fat_percent}
- `update_day_today` - изменить план только на сегодня: {meals, workout, supplements, tasks}
- `update_weekly_template` - изменить шаблон недели (только при явной команде): {monday: {...}, ...}
- `save_inbody` - InBody/PICOOC данные: {weight, muscle_mass, fat_mass, fat_percent, bmr, body_water, visceral_fat}
- `add_product` - добавить продукт в базу: {name, calories_per_100g, protein, fat, carbs, standard_portion_g, group}
- `mark_product_always` - отметить как постоянный продукт (после подтверждения): {name}
- `mark_product_frequent` - отметить как частый: {name}
- `mark_product_oneoff` - перевести в редкие: {name}
- `set_workout_schedule` - расписание тренировок: {monday: {name, time}, ...}

Если действие не нужно — actions: []. reply ВСЕГДА должен быть.

Если пользователь только что прошёл онбординг — собери все данные и сделай update_profile + update_nutrition_plan + set_workout_schedule одной пачкой действий."""


class AIHandler:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-5"
        groq_key = os.getenv("GROQ_API_KEY")
        self.groq = Groq(api_key=groq_key) if groq_key else None

    def _date_context(self) -> str:
        now = datetime.now()
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return f"Сегодня: {days[now.weekday()]}, {now.strftime('%d.%m.%Y')}, время: {now.strftime('%H:%M')}."

    def _parse_json(self, text: str) -> Optional[dict]:
        """Robust JSON extraction"""
        text = text.strip()
        # Remove code fences
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        # Find JSON object
        start = text.find("{")
        if start < 0:
            return None
        # Match braces
        depth = 0
        end = -1
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return None
        try:
            return json.loads(text[start:end])
        except Exception as e:
            logger.error(f"JSON parse error: {e}")
            return None

    def _build_system_prompt(self, full_context: dict) -> str:
        ctx_json = json.dumps(full_context, ensure_ascii=False, default=str, indent=2)
        return f"{SYSTEM_PROMPT}\n\n{self._date_context()}\n\nКОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:\n{ctx_json}"

    async def process_message(
        self,
        text: str,
        full_context: dict,
        chat_history: list,
    ) -> dict:
        """Main entry point - process any text message"""
        system = self._build_system_prompt(full_context)

        messages = []
        for msg in chat_history[-30:]:  # last 30 messages
            messages.append(msg)
        messages.append({"role": "user", "content": text})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=system,
                messages=messages,
            )
            raw = response.content[0].text
        except Exception as e:
            logger.error(f"Claude error: {e}")
            return {"actions": [], "reply": "Ошибка соединения, попробуй ещё раз."}

        result = self._parse_json(raw)
        if not result:
            # Fallback - treat as plain reply
            return {"actions": [], "reply": raw[:1000]}

        return {
            "actions": result.get("actions", []),
            "reply": result.get("reply", ""),
        }

    async def analyze_image_unified(self, img_bytes: bytes, full_context: dict) -> dict:
        """Analyze any image - food, label, FatSecret, InBody, PICOOC"""
        img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        system = self._build_system_prompt(full_context)

        instruction = """Проанализируй изображение. Это может быть:
- Скрин из FatSecret (дневной итог или один приём)
- Этикетка продукта
- Фото еды
- Отчёт InBody / PICOOC
- Что-то ещё связанное с питанием/тренировками

Определи что это, выдели данные и верни JSON с actions и reply.

Если это **дневной итог из FatSecret** (видны несколько приёмов пищи и общая сумма дня) — используй action `meals_replace_today`. Также добавь все уникальные продукты через `add_product`.

Если это **планирование** (пользователь явно написал что планирует) — используй `plan_meal`.

Если это **InBody/PICOOC** — используй `save_inbody` со всеми данными которые видишь."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=3000,
                system=system,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                        {"type": "text", "text": instruction}
                    ]
                }]
            )
            raw = response.content[0].text
        except Exception as e:
            logger.error(f"Image analysis error: {e}")
            return {"actions": [], "reply": "Не смог проанализировать изображение."}

        result = self._parse_json(raw)
        if not result:
            return {"actions": [], "reply": raw[:1000]}
        return result

    async def transcribe_voice(self, audio_path: str) -> Optional[str]:
        if not self.groq:
            logger.error("Groq not configured")
            return None
        try:
            with open(audio_path, "rb") as f:
                result = self.groq.audio.transcriptions.create(
                    model="whisper-large-v3",
                    file=f,
                    language="ru"
                )
            return result.text.strip()
        except Exception as e:
            logger.error(f"Whisper error: {e}")
            return None

    async def generate_daily_summary(
        self, today_meals, today_totals, plan, activity, had_workout, weight_today
    ) -> str:
        meals_str = "\n".join([f"  • {m['time']} {m['description']} — {m['calories']} ккал" for m in today_meals]) or "  Записей нет"
        cal_diff = today_totals.get('calories', 0) - plan.get('calories', 0)
        prot_diff = today_totals.get('protein', 0) - plan.get('protein', 0)
        steps = activity.get('steps', 0) if activity else 0

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": f"""Напиши краткий итог дня для пользователя на сушке. Тон: тренер, без морализаторства.

ПИТАНИЕ:
{meals_str}
Итого: {today_totals.get('calories', 0)} ккал ({'+' if cal_diff >=0 else ''}{cal_diff} от плана)
Белок: {today_totals.get('protein', 0)}/{plan.get('protein', 0)}г

АКТИВНОСТЬ:
{'Тренировка была' if had_workout else 'Без тренировки'}, шаги: {steps:,}
{'Вес сегодня: ' + str(weight_today) + ' кг' if weight_today else ''}

3-4 предложения: оценка дня, что хорошо, что подтянуть. Только текст."""}]
            )
            return response.content[0].text
        except Exception as e:
            logger.error(f"Summary error: {e}")
            return "Итог дня недоступен."

    async def generate_text_analytics(
        self, period, week_data, month_data, plan, weight_history, activity_data
    ) -> str:
        def avg(lst, key):
            vals = [d[key] for d in lst if d.get(key, 0) > 0]
            return round(sum(vals) / len(vals)) if vals else 0

        data = month_data if period == 'month' else week_data
        avg_cal = avg(data, 'calories')
        avg_prot = avg(data, 'protein')

        weight_change = ""
        if len(weight_history) >= 2:
            diff = round(weight_history[0]['weight'] - weight_history[-1]['weight'], 1)
            weight_change = f"Вес: {'+' if diff > 0 else ''}{diff} кг"

        avg_steps = avg(activity_data, 'steps') if activity_data else 0

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=600,
                messages=[{"role": "user", "content": f"""Аналитика за месяц для пользователя на сушке.

ДАННЫЕ:
- Калории среднее: {avg_cal} ккал (цель: {plan.get('calories', 0)})
- Белок среднее: {avg_prot}г (цель: {plan.get('protein', 0)})
- {weight_change}
- Шаги: {avg_steps:,}/день в среднем

Структура:
1. Общая оценка
2. Что работает
3. Главная проблема и совет
4. Корреляции
5. Цель на месяц

150-200 слов. Только текст."""}]
            )
            return response.content[0].text
        except Exception as e:
            return "Аналитика недоступна."
