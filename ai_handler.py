import anthropic
import base64
import json
import os
from typing import Optional
from groq import Groq


class AIHandler:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = "claude-opus-4-5"
        groq_key = os.getenv("GROQ_API_KEY")
        self.openai = Groq(api_key=groq_key) if groq_key else None

    def _parse_json(self, text: str) -> Optional[dict]:
        try:
            clean = text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(clean)
        except Exception:
            return None

    async def parse_nutrition_plan(self, text: str) -> Optional[dict]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""Извлеки план питания из текста и верни ТОЛЬКО JSON без пояснений:\n"
{{
  "calories": число,
  "protein": число_в_граммах,
  "fat": число_в_граммах,
  "carbs": число_в_граммах
}}

Текст: {text}"""
            }]
        )
        return self._parse_json(response.content[0].text)

    async def parse_workout_schedule(self, text: str) -> Optional[dict]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"""Извлеки расписание тренировок и верни ТОЛЬКО JSON без пояснений.\n"
Используй английские названия дней: monday, tuesday, wednesday, thursday, friday, saturday, sunday.
Формат:
{{
  "monday": {{"name": "Грудь и трицепс", "time": "18:00"}},
  "wednesday": {{"name": "Спина и бицепс", "time": "18:00"}}
}}
Включай только тренировочные дни.

Текст: {text}"""
            }]
        )
        return self._parse_json(response.content[0].text)

    async def analyze_food_photo(
        self,
        photo_bytes: bytes,
        today_totals: Optional[dict] = None,
        plan: Optional[dict] = None
    ) -> Optional[dict]:
        """\n"
        Analyzes any food photo:
        - FatSecret screenshot → exact numbers
        - Nutrition label on product → per-serving numbers + fit analysis
        - Photo of actual food → estimated macros
        Returns dict with nutrition data + optional advice if it's a label scan.
        """
        photo_b64 = base64.standard_b64encode(photo_bytes).decode("utf-8")

        context = ""
        if plan and today_totals:
            cal_left = plan['calories'] - today_totals.get('calories', 0)
            prot_left = plan['protein'] - today_totals.get('protein', 0)
            fat_left = plan['fat'] - today_totals.get('fat', 0)
            carbs_left = plan['carbs'] - today_totals.get('carbs', 0)
            context = f"""\n"
Контекст пользователя:
- Дневной план: {plan['calories']} ккал | Б:{plan['protein']}г Ж:{plan['fat']}г У:{plan['carbs']}г
- Осталось сегодня: {cal_left} ккал | Б:{prot_left}г Ж:{fat_left}г У:{carbs_left}г
"""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=700,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": photo_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": f"""Проанализируй фото и определи что это:"

1. СКРИН ИЗ FATSECRET — дневник питания или запись еды
2. ЭТИКЕТКА / ТАБЛИЦА КБЖУ на продукте — пользователь планирует это съесть
3. ФОТО ЕДЫ — реальная еда, нужно оценить состав

{context}

Верни ТОЛЬКО JSON без пояснений:
{{
  "photo_type": "fatsecret" | "label" | "food",
  "description": "название продукта/блюда",
  "serving_size": "размер порции если есть на этикетке, иначе null",
  "calories": число на порцию,
  "protein": число_г,
  "fat": число_г,
  "carbs": число_г,
  "comment": "живой короткий комментарий тренера на русском — для фото еды: что видишь, оценка выбора (1-2 предложения, можно с лёгким юмором). Для FatSecret: короткая оценка дня. Для этикетки: null",
  "fit_analysis": "если это этикетка И есть контекст плана — напиши 2-3 предложения: вписывается ли продукт в остаток дня, что можно подкорректировать. Если контекста нет или это не этикетка — null"
}}

Для этикетки бери цифры НА ПОРЦИЮ (или на 100г если порция не указана).
Для FatSecret — суммарные цифры из скрина.
Для фото еды — оцени примерно."""
                    }
                ]
            }]
        )
        return self._parse_json(response.content[0].text)

    async def classify_and_handle(
        self,
        text: str,
        today_totals: Optional[dict],
        plan: Optional[dict],
        schedule: Optional[dict],
        recent_logs: Optional[dict]
    ) -> dict:
        """\n"
        Classify user message and return appropriate response.
        Types: meal | workout | weight | workout_reschedule | question
        """
        context = self._build_context(today_totals, plan, schedule, recent_logs)

        system_prompt = f"""Ты персональный фитнес-ассистент. Отвечай только на русском языке."

КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:
{context}

Твои задачи:
1. Определить тип сообщения и вернуть JSON
2. Для вопросов — дать умный совет с учётом контекста

Возможные типы ответа:

ЕДА: {{"type": "meal", "data": {{"description": "...", "calories": N, "protein": N, "fat": N, "carbs": N}}}}

ТРЕНИРОВКА: {{"type": "workout", "data": {{"type": "название", "summary": "краткое описание", "exercises": [{{"name": "...", "sets": N, "reps": N, "weight": N}}]}}}}

ВЕС/ЗАМЕРЫ: {{"type": "weight", "data": {{"weight": N}}}}

СДВИГ ТРЕНИРОВКИ: {{"type": "workout_reschedule", "data": {{"new_time": "HH:MM", "day": "сегодня/завтра"}}}}

ДОБАВИТЬ ТАБЛЕТКУ/БАД: {{"type": "add_supplement", "data": {{"name": "название", "dose": "дозировка", "timing": "before_meal|after_meal|with_meal|independent", "time_of_day": "HH:MM или null"}}}}

ДОБАВИТЬ ЗАДАЧУ: {{"type": "add_task", "data": {{"title": "название задачи", "time_str": "HH:MM или null", "repeat": "daily|none"}}}}

ДАННЫЕ ИЗ APPLE HEALTH (от Shortcuts) — утренняя или вечерняя сводка:
{{"type": "health_sync", "data": {{
  "sync_type": "morning" | "evening",
  "steps": число_или_0,
  "steps_date": "yesterday" | "today",
  "calories_burned": число_или_0,
  "active_minutes": число_или_0,
  "weight": число_или_null,
  "weight_measured_at": "время взвешивания строкой или null",
  "workouts": [{{"type": "название", "duration_min": число, "calories": число}}],
  "source": "shortcuts"
}}}}
Признаки утренней сводки: есть вес, упоминается "утро", "взвесился", "вчерашние шаги", время ~11:00.
Признаки вечерней сводки: нет веса, упоминается "вечер", "сегодняшние шаги", время ~23:00.

ВОПРОС/СОВЕТ: {{"type": "question", "answer": "твой развёрнутый ответ с учётом данных пользователя"}}

Верни ТОЛЬКО JSON без пояснений."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=system_prompt,
            messages=[{"role": "user", "content": text}]
        )

        result = self._parse_json(response.content[0].text)
        if not result:
            return {"type": "unknown"}
        return result

    async def recalculate_meal_timing(
        self,
        nutrition_plan: Optional[dict],
        workout_time: str,
        today_logs: Optional[dict]
    ) -> str:
        plan_info = ""
        if nutrition_plan:
            plan_info = f"Калории: {nutrition_plan['calories']} ккал, Б:{nutrition_plan['protein']}г Ж:{nutrition_plan['fat']}г У:{nutrition_plan['carbs']}г"

        already_eaten = ""
        if today_logs and today_logs.get('meals'):
            meals = today_logs['meals']
            already_eaten = "Уже съедено сегодня:\n" + "\n".join(
                [f"• {m['time']} {m['description']} — {m['calories']} ккал" for m in meals[:5]]
            )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"""Тренировка сдвинута на {workout_time}.\n"
План питания: {plan_info}
{already_eaten}

Составь рекомендации по времени приёмов пищи с учётом новой тренировки.
Укажи что и когда есть до и после тренировки.
Отвечай кратко, по делу, на русском языке."""
            }]
        )
        return response.content[0].text

    async def suggest_kbju_adjustment(
        self,
        current_plan: dict,
        plateau_info: dict,
        profile: dict | None,
        reason: str = "plateau"
    ) -> dict:
        """Suggest new KBJU plan based on plateau or weight change"""
        profile_str = ""
        if profile:
            profile_str = f"Профиль: вес {profile.get('weight')}кг, цель: {profile.get('goal')}, активность: {profile.get('activity')}"

        response = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": f"""Пользователь на сушке столкнулся с плато в весе.\n"
{profile_str}
Текущий план: {current_plan['calories']} ккал, Б:{current_plan['protein']}г Ж:{current_plan['fat']}г У:{current_plan['carbs']}г
Плато: вес не менялся ~{plateau_info.get('days', 14)} дней (было {plateau_info.get('avg_older')}кг, сейчас {plateau_info.get('avg_recent')}кг)

Предложи скорректированный план. Верни JSON:
{{
  "text": "объяснение на русском — что предлагаешь и почему (2-3 предложения)",
  "new_plan": {{
    "calories": число,
    "protein": число,
    "fat": число,
    "carbs": число
  }}
}}"""
            }]
        )
        result = self._parse_json(response.content[0].text)
        if not result:
            # fallback
            new_cal = current_plan['calories'] - 150
            return {
                "text": f"Предлагаю снизить калории на 150 ккал (до {new_cal} ккал) за счёт углеводов. Белок оставляем прежним для сохранения мышц.",
                "new_plan": {
                    "calories": new_cal,
                    "protein": current_plan['protein'],
                    "fat": current_plan['fat'],
                    "carbs": current_plan['carbs'] - round(150/4),
                }
            }
        return result

    async def generate_weekly_summary(
        self,
        week_data: list,
        plan: dict,
        weights: list
    ) -> str:
        """Generate weekly summary with AI commentary"""
        avg_cal = round(sum(d['calories'] for d in week_data) / len(week_data)) if week_data else 0
        avg_prot = round(sum(d['protein'] for d in week_data) / len(week_data)) if week_data else 0
        days_logged = len(week_data)

        weight_change = ""
        if len(weights) >= 2:
            diff = round(weights[0]['weight'] - weights[-1]['weight'], 1)
            weight_change = f"Вес: {weights[-1]['weight']}кг → {weights[0]['weight']}кг (изменение: {'+' if diff>0 else ''}{diff}кг)"

        response = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": f"""Напиши краткий итог недели для пользователя на сушке. Тон: дружелюбный тренер."

Данные:
- Дней с записями: {days_logged}/7
- Средние калории: {avg_cal} ккал (план: {plan['calories']} ккал)
- Средний белок: {avg_prot}г (план: {plan['protein']}г)
- {weight_change}

Напиши 3-4 предложения: что хорошо, что подтянуть, мотивация. Только текст, без JSON."""
            }]
        )
        return response.content[0].text

    async def check_product_fit(
        self,
        product_text: str,
        today_totals: dict | None,
        plan: dict | None
    ) -> str:
        """Answer 'can I eat X?' questions"""
        context = ""
        if plan and today_totals:
            cal_left = plan['calories'] - today_totals.get('calories', 0)
            prot_left = plan['protein'] - today_totals.get('protein', 0)
            fat_left = plan['fat'] - today_totals.get('fat', 0)
            carbs_left = plan['carbs'] - today_totals.get('carbs', 0)
            context = f"Осталось сегодня: {cal_left} ккал | Б:{prot_left}г Ж:{fat_left}г У:{carbs_left}г"

        response = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""Пользователь спрашивает: можно ли съесть "{product_text}"?\n"
{context}

Ответь коротко на русском:
1. Примерные КБЖУ этого продукта
2. Вписывается ли в остаток дня
3. Если нет — что можно убрать чтобы вписался

Тон: дружелюбный тренер, 2-4 предложения."""
            }]
        )
        return response.content[0].text


    async def suggest_meal(
        self,
        meal_type: str,
        today_totals: dict | None,
        plan: dict | None,
        meal_history: list,
    ) -> str:
        """Suggest meal options based on user's eating history and today's remaining macros"""

        # Build remaining macros context
        remaining = ""
        if plan and today_totals:
            cal_left = plan['calories'] - today_totals.get('calories', 0)
            prot_left = plan['protein'] - today_totals.get('protein', 0)
            fat_left = plan['fat'] - today_totals.get('fat', 0)
            carbs_left = plan['carbs'] - today_totals.get('carbs', 0)
            remaining = f"Осталось на сегодня: {cal_left} ккал | Б:{prot_left}г Ж:{fat_left}г У:{carbs_left}г"

        # Extract frequent foods from history
        food_freq = {}
        for meal in meal_history:
            desc = meal.get('description', '').strip()
            if desc and len(desc) > 2:
                food_freq[desc] = food_freq.get(desc, 0) + 1

        # Top 15 most frequent
        top_foods = sorted(food_freq.items(), key=lambda x: -x[1])[:15]
        history_str = ", ".join([f"{name} (×{cnt})" for name, cnt in top_foods]) if top_foods else "история пока пуста"

        response = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": f"""Пользователь на сушке спрашивает что поесть на {meal_type}."

{remaining}

Продукты и блюда которые он обычно ест (из истории за 30 дней):
{history_str}

Предложи 2-3 конкретных варианта ТОЛЬКО из его привычных продуктов.
Для каждого варианта укажи примерные КБЖУ и почему он подходит под остаток.
Если ничего не вписывается — скажи честно и предложи уменьшить порцию.
Тон: дружелюбный тренер, коротко и по делу. Только текст, без JSON."""
            }]
        )
        return response.content[0].text


    async def transcribe_voice(self, audio_path: str) -> str | None:
        """Transcribe voice message using OpenAI Whisper"""
        if not self.openai:
            return None
        try:
            with open(audio_path, "rb") as audio_file:
                result = self.openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="ru"
                )
            return result.text.strip()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Whisper error: {e}")
            return None


    async def generate_daily_summary(
        self,
        today_meals: list,
        today_totals: dict,
        plan: dict,
        activity: dict | None,
        had_workout: bool,
        weight_today: float | None,
    ) -> str:
        """Evening daily summary — friendly coach tone"""
        meals_str = "\n".join([
            f"  • {m['time']} {m['description']} — {m['calories']} ккал"
            for m in today_meals
        ]) or "  Записей нет"

        cal_diff = today_totals.get('calories', 0) - plan.get('calories', 0)
        prot_diff = today_totals.get('protein', 0) - plan.get('protein', 0)
        steps = activity.get('steps', 0) if activity else 0
        burned = activity.get('calories_burned', 0) if activity else 0

        response = self.client.messages.create(
            model=self.model,
            max_tokens=450,
            messages=[{"role": "user", "content": f"""Напиши итог дня для пользователя на сушке.\n"
Тон: тренер — честный, краткий, с лёгким юмором если день был хорошим.

ПИТАНИЕ:
{meals_str}
Итого: {today_totals.get('calories', 0)} ккал (план {plan.get('calories', 0)}, {'перебор' if cal_diff > 0 else 'дефицит'} {abs(cal_diff)} ккал)
Белок: {today_totals.get('protein', 0)}г из {plan.get('protein', 0)}г ({'✅' if prot_diff >= -10 else '❌ не добрал'})

АКТИВНОСТЬ:
{'Была тренировка' if had_workout else 'Тренировки не было'}
Шаги: {steps:,} | Сожжено: {burned} ккал

{'Вес сегодня: ' + str(weight_today) + ' кг' if weight_today else ''}

Напиши 3-5 предложений: оценка дня, что молодец, что подтянуть завтра. Только текст."""}]
        )
        return response.content[0].text

    async def generate_text_analytics(
        self,
        period: str,
        week_data: list,
        month_data: list,
        plan: dict,
        weight_history: list,
        activity_data: list,
    ) -> str:
        """Text analytics with correlations, best/worst days, averages"""
        def avg(lst, key):
            vals = [d[key] for d in lst if d.get(key, 0) > 0]
            return round(sum(vals) / len(vals)) if vals else 0

        data = month_data if period == 'month' else week_data
        label = "месяц" if period == 'month' else "неделю"

        avg_cal = avg(data, 'calories')
        avg_prot = avg(data, 'protein')
        best_day = max(data, key=lambda d: d.get('protein', 0), default=None)
        worst_day = max(data, key=lambda d: abs(d.get('calories', 0) - plan.get('calories', 1)), default=None)

        weight_change = ""
        if len(weight_history) >= 2:
            diff = round(weight_history[0]['weight'] - weight_history[-1]['weight'], 1)
            weight_change = f"Вес изменился на {'+' if diff > 0 else ''}{diff} кг"

        avg_steps = avg(activity_data, 'steps') if activity_data else 0

        response = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            messages=[{"role": "user", "content": f"""Напиши аналитику за {label} для пользователя на сушке."

ДАННЫЕ:
- Средние калории: {avg_cal} ккал (план: {plan.get('calories', 0)} ккал)
- Средний белок: {avg_prot}г (план: {plan.get('protein', 0)}г)
- Лучший день по белку: {best_day['date'] if best_day else 'н/д'} ({best_day.get('protein', 0) if best_day else 0}г)
- День с наибольшим отклонением: {worst_day['date'] if worst_day else 'н/д'}
- {weight_change}
- Средние шаги: {avg_steps:,}/день

Напиши структурированный анализ:
1. Общая оценка периода
2. Что работает хорошо
3. Главная проблема и конкретный совет
4. Корреляции — что замечаешь между данными
5. Цель на следующий период

Только текст, без JSON. 150-200 слов."""}]
        )
        return response.content[0].text


    async def analyze_inbody_photo(self, photo_bytes: bytes) -> dict | None:
        """Extract InBody report data from photo"""
        photo_b64 = base64.standard_b64encode(photo_bytes).decode("utf-8")
        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": photo_b64}
                    },
                    {
                        "type": "text",
                        "text": """Это отчёт InBody (анализ состава тела).\n"
Извлеки все доступные данные и верни ТОЛЬКО JSON без пояснений:
{
  "weight": число или null,
  "muscle_mass": число_кг или null,
  "fat_mass": число_кг или null,
  "fat_percent": число_% или null,
  "bmr": число_ккал или null,
  "body_water": число_кг или null,
  "visceral_fat": число или null,
  "found": true если данные найдены, false если это не InBody
}
Все числа без единиц измерения, только цифры."""
                    }
                ]
            }]
        )
        result = self._parse_json(response.content[0].text)
        if result and result.get('found'):
            return result
        return None

    async def calculate_kbju_from_inbody(
        self,
        inbody: dict,
        goal: str,
        activity: str,
    ) -> dict:
        """Calculate precise KBJU from InBody data"""
        ACTIVITY_MULTIPLIERS = {
            "minimal": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725
        }

        # Katch-McArdle formula — more accurate when we know lean mass
        lean_mass = inbody.get('muscle_mass')
        weight = inbody.get('weight')
        fat_percent = inbody.get('fat_percent')
        bmr_from_report = inbody.get('bmr')

        if bmr_from_report:
            bmr = bmr_from_report
        elif lean_mass:
            bmr = 370 + 21.6 * lean_mass  # Katch-McArdle
        elif weight and fat_percent:
            lean = weight * (1 - fat_percent / 100)
            bmr = 370 + 21.6 * lean
        else:
            bmr = 1800  # fallback

        multiplier = ACTIVITY_MULTIPLIERS.get(activity, 1.55)
        tdee = bmr * multiplier

        if goal == 'cut':
            calories = round(tdee * 0.8)
            protein_per_kg = 2.4  # higher on cut to preserve muscle
        elif goal == 'bulk':
            calories = round(tdee * 1.1)
            protein_per_kg = 2.0
        else:
            calories = round(tdee)
            protein_per_kg = 1.8

        protein = round((lean_mass or weight or 70) * protein_per_kg)
        fat = round(calories * 0.25 / 9)
        carbs = round((calories - protein * 4 - fat * 9) / 4)

        return {
            "calories": calories,
            "protein": protein,
            "fat": fat,
            "carbs": carbs,
            "bmr": round(bmr),
            "tdee": round(tdee),
        }

    async def format_inbody_summary(self, inbody: dict, kbju: dict, goal: str) -> str:
        """Generate readable InBody analysis"""
        fat_pct = inbody.get('fat_percent', '?')
        muscle = inbody.get('muscle_mass', '?')
        fat_mass = inbody.get('fat_mass', '?')
        weight = inbody.get('weight', '?')
        bmr = kbju.get('bmr', '?')
        tdee = kbju.get('tdee', '?')
        goal_text = {"cut": "сушка", "bulk": "набор", "maintain": "поддержание"}.get(goal, goal)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=350,
            messages=[{"role": "user", "content": f"""Дай краткий анализ состава тела по данным InBody.\n"
Тон: тренер, по делу, мотивирующе.

Данные:
- Вес: {weight} кг
- Мышечная масса: {muscle} кг
- Жировая масса: {fat_mass} кг ({fat_pct}%)
- Базовый метаболизм (BMR): {bmr} ккал
- Суточный расход (TDEE): {tdee} ккал
- Цель: {goal_text}

Рассчитанный план:
- Калории: {kbju['calories']} ккал
- Белок: {kbju['protein']}г | Жиры: {kbju['fat']}г | Углеводы: {kbju['carbs']}г

Напиши 3-4 предложения: оценка состава тела, почему именно такой КБЖУ, на что обратить внимание. Только текст."""}]
        )
        return response.content[0].text


    async def suggest_meal_with_groups(
        self,
        meal_type: str,
        today_totals: dict | None,
        plan: dict | None,
        always_products: list,
        frequent_products: list,
        meal_history: list,
    ) -> str:
        """Suggest meal using product groups — only always+frequent, never oneoff"""
        remaining = ""
        if plan and today_totals:
            cal_left = plan['calories'] - today_totals.get('calories', 0)
            prot_left = plan['protein'] - today_totals.get('protein', 0)
            fat_left = plan['fat'] - today_totals.get('fat', 0)
            carbs_left = plan['carbs'] - today_totals.get('carbs', 0)
            remaining = f"Осталось: {cal_left} ккал | Б:{prot_left}г Ж:{fat_left}г У:{carbs_left}г"

        always_str = ", ".join([p['product_name'] for p in always_products]) or "не указаны"
        frequent_str = ", ".join([p['product_name'] for p in frequent_products]) or "не указаны"

        # Also get frequent items from history for context
        food_freq = {}
        for meal in meal_history:
            desc = meal.get('description', '').strip()
            if desc:
                food_freq[desc] = food_freq.get(desc, 0) + 1
        history_top = ", ".join([k for k, v in sorted(food_freq.items(), key=lambda x: -x[1])[:8]])

        response = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            messages=[{"role": "user", "content": f"""Пользователь на сушке спрашивает что поесть на {meal_type}."

{remaining}

Продукты которые ВСЕГДА есть дома (приоритет для предложений):
{always_str}

Продукты которые ЧАСТО есть дома (можно предлагать):
{frequent_str}

Из истории питания также замечены:
{history_top}

ВАЖНО: предлагай ТОЛЬКО из продуктов "всегда" и "часто". Не придумывай другие.
Предложи 2-3 конкретных варианта с КБЖУ под остаток дня.
Тон: дружелюбный тренер, коротко."""}]
        )
        return response.content[0].text

    async def classify_product_group(self, product_name: str, meal_count: int) -> str:
        """Suggest group for a new product based on context"""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=100,
            messages=[{"role": "user", "content": f"""Продукт "{product_name}" употреблялся {meal_count} раз.\n"
Верни ТОЛЬКО одно слово — группу продукта:
- always (всегда в холодильнике, базовый продукт)
- frequent (часто но не всегда)
- oneoff (разовый, случайный)

Ответ:"""}]
        )
        result = response.content[0].text.strip().lower()
        if result in ('always', 'frequent', 'oneoff'):
            return result
        return 'frequent'

    async def add_sleep_to_context(self, sleep_history: list) -> str:
        """Format sleep data for AI context"""
        if not sleep_history:
            return ""
        avg_hours = round(sum(s['hours'] for s in sleep_history) / len(sleep_history), 1)
        last = sleep_history[0]
        return f"СОН: последний {last['hours']}ч ({last['date']}), среднее за период: {avg_hours}ч"

    async def generate_sleep_correlation(
        self,
        sleep_history: list,
        weight_history: list,
        meal_history: list,
    ) -> str:
        """Analyze sleep vs weight/nutrition correlations"""
        if len(sleep_history) < 5:
            return "Недостаточно данных по сну для анализа — нужно минимум 5 записей."

        sleep_by_date = {s['date']: s['hours'] for s in sleep_history}
        weight_by_date = {w['date']: w['weight'] for w in weight_history}

        # Find overlapping dates
        pairs = []
        for date, hours in sleep_by_date.items():
            if date in weight_by_date:
                pairs.append(f"Сон {hours}ч → вес {weight_by_date[date]}кг")

        pairs_str = "\n".join(pairs[:10]) if pairs else "нет пересечений"

        avg_sleep = round(sum(sleep_by_date.values()) / len(sleep_by_date), 1)
        short_sleep_days = [d for d, h in sleep_by_date.items() if h < 6]

        response = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            messages=[{"role": "user", "content": f"""Проанализируй влияние сна на вес и питание пользователя.

Данные сон → вес:
{pairs_str}

Средний сон: {avg_sleep}ч
Дней с коротким сном (<6ч): {len(short_sleep_days)}

Напиши 2-3 предложения: есть ли корреляция, как сон влияет на прогресс, конкретный совет.
Только текст, без JSON."""}]
        )
        return response.content[0].text

    def _build_context(self, today_totals, plan, schedule, recent_logs) -> str:
        lines = []

        if plan:
            lines.append(f"ПЛАН ПИТАНИЯ: {plan['calories']} ккал | Б:{plan['protein']}г Ж:{plan['fat']}г У:{plan['carbs']}г")

        if today_totals and today_totals.get('calories', 0) > 0:
            lines.append(f"СЪЕДЕНО СЕГОДНЯ: {today_totals['calories']} ккал | Б:{today_totals['protein']}г Ж:{today_totals['fat']}г У:{today_totals['carbs']}г")
            if plan:
                left = plan['calories'] - today_totals['calories']
                lines.append(f"ОСТАЛОСЬ: {left} ккал")

        if schedule:
            days_ru = {
                "monday": "Пн", "tuesday": "Вт", "wednesday": "Ср",
                "thursday": "Чт", "friday": "Пт", "saturday": "Сб", "sunday": "Вс"
            }
            sched_str = ", ".join([f"{days_ru.get(d, d)}: {i['name']} {i.get('time', '')}" for d, i in schedule.items()])
            lines.append(f"РАСПИСАНИЕ ТРЕНИРОВОК: {sched_str}")

        if recent_logs:
            if recent_logs.get('workouts'):
                last_w = recent_logs['workouts'][0]
                lines.append(f"ПОСЛЕДНЯЯ ТРЕНИРОВКА: {last_w['date']} — {last_w.get('workout_type', '')} {last_w.get('summary', '')}")
            if recent_logs.get('weights'):
                last_weight = recent_logs['weights'][0]
                lines.append(f"ПОСЛЕДНИЙ ВЕС: {last_weight['weight']} кг ({last_weight['date']})")

        return "\n".join(lines) if lines else "Данных пока нет."
