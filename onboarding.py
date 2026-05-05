"""
Proactive AI monitor.
Runs periodic checks and messages the user when it sees something important:
- Weight plateau (2+ weeks no change)
- Protein deficit (3 days in a row)
- Calorie overrun (3 days in a row)
- Weekly summary (every Sunday)
- Monthly KBJU recalculation suggestion
"""
import logging
from datetime import date, timedelta
from telegram.ext import ContextTypes
from database import Database
from ai_handler import AIHandler

logger = logging.getLogger(__name__)


class ProactiveMonitor:
    def __init__(self, db: Database, ai: AIHandler, bot):
        self.db = db
        self.ai = ai
        self.bot = bot

    async def run_all_checks(self, context: ContextTypes.DEFAULT_TYPE = None):
        """Called by JobQueue — runs all checks for all users"""
        users = self.db.get_all_users()
        for user in users:
            user_id = user['user_id']
            try:
                await self._check_user(user_id)
            except Exception as e:
                logger.error(f"Proactive check failed for {user_id}: {e}")

    async def _check_user(self, user_id: int):
        today = date.today()
        plan = self.db.get_nutrition_plan(user_id)
        if not plan:
            return  # Not set up yet

        # Don't spam — check what we already notified about
        last_notifs = self.db.get_last_notifications(user_id)

        # 1. Weekly summary — every Sunday
        if today.weekday() == 6:  # Sunday
            last_weekly = last_notifs.get('weekly_summary')
            if last_weekly != today.isoformat():
                await self._send_weekly_summary(user_id, plan)
                self.db.save_notification(user_id, 'weekly_summary', today.isoformat())
            return  # Only one notification per day

        # 2. Weight plateau check
        last_plateau = last_notifs.get('plateau_warning')
        if last_plateau != today.isoformat():
            plateau = self._check_plateau(user_id)
            if plateau:
                await self._send_plateau_warning(user_id, plateau, plan)
                self.db.save_notification(user_id, 'plateau_warning', today.isoformat())
                return

        # 3. Low protein 3 days in a row
        last_protein = last_notifs.get('low_protein')
        days_since_protein = self._days_since(last_protein)
        if days_since_protein is None or days_since_protein >= 3:
            low_days = self._check_low_protein(user_id, plan, days=3)
            if low_days >= 3:
                await self._send_low_protein_warning(user_id, low_days, plan)
                self.db.save_notification(user_id, 'low_protein', today.isoformat())
                return

        # 4. Calorie overrun 3 days in a row
        last_overrun = last_notifs.get('calorie_overrun')
        days_since_overrun = self._days_since(last_overrun)
        if days_since_overrun is None or days_since_overrun >= 3:
            overrun_days = self._check_calorie_overrun(user_id, plan, days=3)
            if overrun_days >= 3:
                await self._send_overrun_warning(user_id, overrun_days, plan)
                self.db.save_notification(user_id, 'calorie_overrun', today.isoformat())
                return

        # 5. Monthly KBJU recalculation
        last_recheck = last_notifs.get('kbju_recheck')
        days_since_recheck = self._days_since(last_recheck)
        if days_since_recheck is None or days_since_recheck >= 30:
            await self._suggest_kbju_recheck(user_id, plan)
            self.db.save_notification(user_id, 'kbju_recheck', today.isoformat())

    def _days_since(self, date_str) -> int | None:
        if not date_str:
            return None
        try:
            d = date.fromisoformat(date_str)
            return (date.today() - d).days
        except Exception:
            return None

    def _check_plateau(self, user_id: int) -> dict | None:
        """Returns plateau info if weight hasn't changed in 14+ days"""
        weights = self.db.get_weight_history(user_id, days=21)
        if len(weights) < 4:
            return None

        recent = [w['weight'] for w in weights[:7]]    # last 7 days
        older = [w['weight'] for w in weights[7:14]]   # 7-14 days ago

        if not recent or not older:
            return None

        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)
        change = avg_recent - avg_older

        # Less than 0.3kg change over 2 weeks = plateau
        if abs(change) < 0.3:
            return {
                'avg_recent': round(avg_recent, 1),
                'avg_older': round(avg_older, 1),
                'change': round(change, 1),
                'days': len(weights),
            }
        return None

    def _check_low_protein(self, user_id: int, plan: dict, days: int = 3) -> int:
        """Returns number of consecutive days with protein below 85% of target"""
        target = plan.get('protein', 0)
        if not target:
            return 0
        stats = self.db.get_week_stats(user_id)
        streak = 0
        for day in stats[:days]:
            if day['protein'] < target * 0.85:
                streak += 1
            else:
                break
        return streak

    def _check_calorie_overrun(self, user_id: int, plan: dict, days: int = 3) -> int:
        """Returns number of consecutive days over calories by 10%+"""
        target = plan.get('calories', 0)
        if not target:
            return 0
        stats = self.db.get_week_stats(user_id)
        streak = 0
        for day in stats[:days]:
            if day['calories'] > target * 1.10:
                streak += 1
            else:
                break
        return streak

    async def _send_plateau_warning(self, user_id: int, plateau: dict, plan: dict):
        profile = self.db.get_user_profile(user_id)
        suggestion = await self.ai.suggest_kbju_adjustment(
            current_plan=plan,
            plateau_info=plateau,
            profile=profile,
            reason="plateau"
        )
        text = (
            f"📊 *Заметил кое-что важное*\n\n"
            f"Вес практически не меняется уже 2 недели "
            f"(было ~{plateau['avg_older']}кг, сейчас ~{plateau['avg_recent']}кг).\n\n"
            f"🤖 *Предлагаю скорректировать план:*\n{suggestion['text']}\n\n"
            f"Применить новый план?"
        )
        await self.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown"
        )
        # Store pending plan change
        self.db.save_pending_plan(user_id, suggestion['new_plan'], reason="plateau")

    async def _send_low_protein_warning(self, user_id: int, days: int, plan: dict):
        text = (
            f"⚠️ *{days} дня подряд не добираешь белок*\n\n"
            f"Цель: {plan['protein']}г, а по факту значительно меньше.\n\n"
            f"При сушке это критично — тело начинает разрушать мышцы.\n\n"
            f"💡 Добавь в рацион: куриная грудка 150г (+35г белка), "
            f"творог 0% 200г (+30г белка), яйца 3шт (+18г белка)."
        )
        await self.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")

    async def _send_overrun_warning(self, user_id: int, days: int, plan: dict):
        text = (
            f"⚠️ *{days} дня подряд перебор по калориям*\n\n"
            f"Цель: {plan['calories']} ккал — но последние дни выходишь за рамки.\n\n"
            f"Это нормально бывает, но {days} дня подряд начинают влиять на прогресс. "
            f"Посмотри на вечерние приёмы пищи — чаще всего перебор именно там 🌙"
        )
        await self.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")

    async def _send_weekly_summary(self, user_id: int, plan: dict):
        week_data = self.db.get_week_stats(user_id)
        weights = self.db.get_weight_history(user_id, days=7)

        if not week_data:
            return

        summary = await self.ai.generate_weekly_summary(
            week_data=week_data,
            plan=plan,
            weights=weights
        )
        await self.bot.send_message(
            chat_id=user_id,
            text=f"📅 *Итоги недели*\n\n{summary}",
            parse_mode="Markdown"
        )

    async def _suggest_kbju_recheck(self, user_id: int, plan: dict):
        weights = self.db.get_weight_history(user_id, days=30)
        if not weights or len(weights) < 3:
            return

        first_weight = weights[-1]['weight']
        last_weight = weights[0]['weight']
        change = last_weight - first_weight

        if abs(change) < 1.0:
            return  # Not enough change to warrant recalculation

        direction = "снизился" if change < 0 else "вырос"
        text = (
            f"📊 *Прошёл месяц — пора пересчитать план?*\n\n"
            f"Вес {direction} на {abs(round(change, 1))}кг за последний месяц "
            f"({first_weight}кг → {last_weight}кг).\n\n"
            f"При таком изменении КБЖУ стоит пересчитать — иначе прогресс может замедлиться.\n\n"
            f"Пересчитать план под новый вес?"
        )
        await self.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown"
        )
        self.db.save_pending_plan(user_id, None, reason="weight_change")
