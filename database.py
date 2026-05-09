import sqlite3
import json
from datetime import datetime, date, timedelta
from typing import Optional


class Database:
    def __init__(self, db_path: str = None):
        import os
        if db_path is None:
            if os.path.exists('/data') and os.path.isdir('/data'):
                db_path = '/data/fitness.db'
            else:
                db_path = 'fitness.db'
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id INTEGER PRIMARY KEY,
                    weight REAL,
                    height REAL,
                    age INTEGER,
                    activity TEXT,
                    goal TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS nutrition_plans (
                    user_id INTEGER PRIMARY KEY,
                    calories INTEGER,
                    protein INTEGER,
                    fat INTEGER,
                    carbs INTEGER,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS daily_plan_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    calories INTEGER,
                    protein INTEGER,
                    fat INTEGER,
                    carbs INTEGER,
                    reason TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, date)
                );

                CREATE TABLE IF NOT EXISTS pending_plan_changes (
                    user_id INTEGER PRIMARY KEY,
                    new_plan_json TEXT,
                    reason TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    user_id INTEGER,
                    notification_type TEXT,
                    last_sent TEXT,
                    PRIMARY KEY (user_id, notification_type)
                );

                CREATE TABLE IF NOT EXISTS workout_schedules (
                    user_id INTEGER PRIMARY KEY,
                    schedule_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS meal_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    time TEXT,
                    description TEXT,
                    calories INTEGER,
                    protein REAL,
                    fat REAL,
                    carbs REAL,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS workout_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    time TEXT,
                    workout_type TEXT,
                    summary TEXT,
                    exercises_json TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS weight_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    weight REAL,
                    created_at TEXT DEFAULT (datetime('now'))
                );
            """)

    def ensure_user(self, user_id: int):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
            )

    def save_nutrition_plan(self, user_id: int, plan: dict, is_base: bool = True):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO nutrition_plans (user_id, calories, protein, fat, carbs, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (user_id, plan['calories'], plan['protein'], plan['fat'], plan['carbs']))
        if not is_base:
            # Also save as today-only override
            self.save_daily_override(user_id, plan, reason="manual")

    def save_daily_override(self, user_id: int, plan: dict, reason: str = ""):
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_plan_overrides
                (user_id, date, calories, protein, fat, carbs, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, today, plan['calories'], plan['protein'], plan['fat'], plan['carbs'], reason))

    def get_todays_plan(self, user_id: int) -> dict | None:
        """Returns today override if exists, else base plan"""
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            override = conn.execute(
                "SELECT * FROM daily_plan_overrides WHERE user_id=? AND date=?",
                (user_id, today)
            ).fetchone()
            if override:
                return dict(override)
        return self.get_nutrition_plan(user_id)

    def save_user_profile(self, user_id: int, profile: dict):
        with self._conn() as conn:
            # Add goal_type and goal_value columns if missing
            try:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN goal_type TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN goal_value REAL")
            except Exception:
                pass
            conn.execute("""
                INSERT OR REPLACE INTO user_profiles
                (user_id, weight, height, age, activity, goal, goal_type, goal_value, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (user_id, profile.get('weight'), profile.get('height'),
                  profile.get('age'), profile.get('activity'), profile.get('goal'),
                  profile.get('goal_type'), profile.get('goal_value')))

    def get_user_profile(self, user_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_users(self) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id FROM users").fetchall()
            return [dict(r) for r in rows]

    def get_weight_history(self, user_id: int, days: int = 30) -> list:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT date, weight FROM weight_logs
                WHERE user_id=? AND date>=?
                ORDER BY date DESC
            """, (user_id, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def save_pending_plan(self, user_id: int, new_plan: dict | None, reason: str):
        import json
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pending_plan_changes (user_id, new_plan_json, reason, created_at)
                VALUES (?, ?, ?, datetime('now'))
            """, (user_id, json.dumps(new_plan) if new_plan else None, reason))

    def get_pending_plan(self, user_id: int) -> dict | None:
        import json
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pending_plan_changes WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if result.get('new_plan_json'):
                result['new_plan'] = json.loads(result['new_plan_json'])
            return result

    def clear_pending_plan(self, user_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM pending_plan_changes WHERE user_id=?", (user_id,))

    def get_last_notifications(self, user_id: int) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT notification_type, last_sent FROM notifications WHERE user_id=?",
                (user_id,)
            ).fetchall()
            return {r['notification_type']: r['last_sent'] for r in rows}

    def save_notification(self, user_id: int, notification_type: str, date_str: str):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO notifications (user_id, notification_type, last_sent)
                VALUES (?, ?, ?)
            """, (user_id, notification_type, date_str))

    def get_nutrition_plan(self, user_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM nutrition_plans WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def save_workout_schedule(self, user_id: int, schedule: dict):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO workout_schedules (user_id, schedule_json, updated_at)
                VALUES (?, ?, datetime('now'))
            """, (user_id, json.dumps(schedule, ensure_ascii=False)))

    def get_workout_schedule(self, user_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT schedule_json FROM workout_schedules WHERE user_id = ?", (user_id,)
            ).fetchone()
            return json.loads(row['schedule_json']) if row else None

    def log_meal(self, user_id: int, meal: dict):
        today = date.today().isoformat()
        now = datetime.now().strftime("%H:%M")
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO meal_logs (user_id, date, time, description, calories, protein, fat, carbs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, today, now,
                meal.get('description', ''),
                meal.get('calories', 0),
                meal.get('protein', 0),
                meal.get('fat', 0),
                meal.get('carbs', 0)
            ))

    def log_workout(self, user_id: int, workout: dict):
        today = date.today().isoformat()
        now = datetime.now().strftime("%H:%M")
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO workout_logs (user_id, date, time, workout_type, summary, exercises_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id, today, now,
                workout.get('type', ''),
                workout.get('summary', ''),
                json.dumps(workout.get('exercises', []), ensure_ascii=False)
            ))

    def log_weight(self, user_id: int, weight: float):
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO weight_logs (user_id, date, weight)
                VALUES (?, ?, ?)
            """, (user_id, today, weight))

    def get_today_totals(self, user_id: int) -> Optional[dict]:
        today = date.today().isoformat()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT 
                    COALESCE(SUM(calories), 0) as calories,
                    COALESCE(SUM(protein), 0) as protein,
                    COALESCE(SUM(fat), 0) as fat,
                    COALESCE(SUM(carbs), 0) as carbs
                FROM meal_logs WHERE user_id = ? AND date = ?
            """, (user_id, today)).fetchone()
            return dict(row) if row else None

    def get_today_meals(self, user_id: int) -> list:
        today = date.today().isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT time, description, calories, protein, fat, carbs
                FROM meal_logs WHERE user_id = ? AND date = ?
                ORDER BY time
            """, (user_id, today)).fetchall()
            return [dict(r) for r in rows]

    def get_week_stats(self, user_id: int) -> list:
        today = date.today()
        week_ago = (today - timedelta(days=6)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT 
                    date,
                    COALESCE(SUM(calories), 0) as calories,
                    COALESCE(SUM(protein), 0) as protein,
                    COALESCE(SUM(fat), 0) as fat,
                    COALESCE(SUM(carbs), 0) as carbs
                FROM meal_logs 
                WHERE user_id = ? AND date >= ?
                GROUP BY date
                ORDER BY date DESC
            """, (user_id, week_ago)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_logs(self, user_id: int, days: int = 3) -> dict:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            meals = conn.execute("""
                SELECT date, time, description, calories, protein, fat, carbs
                FROM meal_logs WHERE user_id = ? AND date >= ?
                ORDER BY date DESC, time DESC LIMIT 20
            """, (user_id, cutoff)).fetchall()

            workouts = conn.execute("""
                SELECT date, time, workout_type, summary
                FROM workout_logs WHERE user_id = ? AND date >= ?
                ORDER BY date DESC LIMIT 10
            """, (user_id, cutoff)).fetchall()

            weights = conn.execute("""
                SELECT date, weight FROM weight_logs
                WHERE user_id = ? AND date >= ?
                ORDER BY date DESC LIMIT 5
            """, (user_id, cutoff)).fetchall()

        return {
            "meals": [dict(r) for r in meals],
            "workouts": [dict(r) for r in workouts],
            "weights": [dict(r) for r in weights]
        }

    def log_activity(self, user_id: int, data: dict):
        """Log steps, calories burned, workouts from Health/Shortcuts"""
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            # Create table if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    steps INTEGER DEFAULT 0,
                    calories_burned INTEGER DEFAULT 0,
                    active_minutes INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, date)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO activity_logs
                (user_id, date, steps, calories_burned, active_minutes, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id, today,
                data.get('steps', 0),
                data.get('calories_burned', 0),
                data.get('active_minutes', 0),
                data.get('source', 'manual')
            ))

    def get_today_activity(self, user_id: int) -> dict | None:
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    steps INTEGER DEFAULT 0,
                    calories_burned INTEGER DEFAULT 0,
                    active_minutes INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, date)
                )
            """)
            row = conn.execute(
                "SELECT * FROM activity_logs WHERE user_id=? AND date=?",
                (user_id, today)
            ).fetchone()
            return dict(row) if row else None

    def get_meal_history(self, user_id: int, days: int = 30) -> list:
        """Get all meals from last N days for pattern analysis"""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT description, calories, protein, fat, carbs, date, time
                FROM meal_logs
                WHERE user_id=? AND date>=?
                ORDER BY date DESC
            """, (user_id, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def log_activity_for_date(self, user_id: int, data: dict, for_date: str):
        """Log activity for a specific date (used for yesterday's steps sync)"""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    steps INTEGER DEFAULT 0,
                    calories_burned INTEGER DEFAULT 0,
                    active_minutes INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, date)
                )
            """)
            conn.execute("""
                INSERT INTO activity_logs (user_id, date, steps, calories_burned, active_minutes, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    steps = MAX(steps, excluded.steps),
                    calories_burned = MAX(calories_burned, excluded.calories_burned),
                    source = excluded.source
            """, (
                user_id, for_date,
                data.get('steps', 0),
                data.get('calories_burned', 0),
                data.get('active_minutes', 0),
                data.get('source', 'shortcuts')
            ))

    def log_weight_for_date(self, user_id: int, weight: float, for_date: str, measured_at: str = None):
        """Log weight for a specific date with optional measurement time"""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS weight_logs_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    weight REAL,
                    measured_at TEXT,
                    source TEXT DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # Remove old entry for same date if exists
            conn.execute(
                "DELETE FROM weight_logs WHERE user_id=? AND date=?",
                (user_id, for_date)
            )
            conn.execute(
                "INSERT INTO weight_logs (user_id, date, weight) VALUES (?, ?, ?)",
                (user_id, for_date, weight)
            )

    def get_month_stats(self, user_id: int) -> list:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT date,
                    COALESCE(SUM(calories),0) as calories,
                    COALESCE(SUM(protein),0) as protein,
                    COALESCE(SUM(fat),0) as fat,
                    COALESCE(SUM(carbs),0) as carbs
                FROM meal_logs WHERE user_id=? AND date>=?
                GROUP BY date ORDER BY date DESC
            """, (user_id, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def get_activity_history(self, user_id: int, days: int = 30) -> list:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT date, steps, calories_burned, active_minutes
                    FROM activity_logs WHERE user_id=? AND date>=?
                    ORDER BY date DESC
                """, (user_id, cutoff)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_latest_weight(self, user_id: int) -> float | None:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT weight FROM weight_logs WHERE user_id=?
                ORDER BY date DESC LIMIT 1
            """, (user_id,)).fetchone()
            return row['weight'] if row else None

    def save_inbody(self, user_id: int, data: dict):
        """Save InBody measurement"""
        from datetime import date
        import json
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS inbody_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    weight REAL,
                    muscle_mass REAL,
                    fat_mass REAL,
                    fat_percent REAL,
                    bmr INTEGER,
                    body_water REAL,
                    visceral_fat REAL,
                    raw_json TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                INSERT INTO inbody_logs
                (user_id, date, weight, muscle_mass, fat_mass, fat_percent, bmr, body_water, visceral_fat, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, date.today().isoformat(),
                data.get('weight'), data.get('muscle_mass'),
                data.get('fat_mass'), data.get('fat_percent'),
                data.get('bmr'), data.get('body_water'),
                data.get('visceral_fat'), json.dumps(data)
            ))

    def get_latest_inbody(self, user_id: int) -> dict | None:
        import json
        with self._conn() as conn:
            try:
                row = conn.execute("""
                    SELECT * FROM inbody_logs WHERE user_id=?
                    ORDER BY date DESC LIMIT 1
                """, (user_id,)).fetchone()
                return dict(row) if row else None
            except Exception:
                return None

    def get_inbody_history(self, user_id: int) -> list:
        import json
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT date, weight, muscle_mass, fat_mass, fat_percent
                    FROM inbody_logs WHERE user_id=?
                    ORDER BY date DESC LIMIT 10
                """, (user_id,)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def log_sleep(self, user_id: int, hours: float, quality: str = None, date_str: str = None):
        from datetime import date
        target_date = date_str or date.today().isoformat()
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sleep_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    hours REAL,
                    quality TEXT,
                    source TEXT DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, date)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO sleep_logs (user_id, date, hours, quality, source)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, target_date, hours, quality, 'shortcuts' if date_str else 'manual'))

    def get_sleep_history(self, user_id: int, days: int = 30) -> list:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT date, hours, quality, source FROM sleep_logs
                    WHERE user_id=? AND date>=? ORDER BY date DESC
                """, (user_id, cutoff)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_last_sleep(self, user_id: int) -> dict | None:
        with self._conn() as conn:
            try:
                row = conn.execute("""
                    SELECT date, hours, quality FROM sleep_logs
                    WHERE user_id=? ORDER BY date DESC LIMIT 1
                """, (user_id,)).fetchone()
                return dict(row) if row else None
            except Exception:
                return None

    # --- Product groups ---
    def save_product_group(self, user_id: int, product_name: str, group: str):
        """group: 'always' | 'frequent' | 'oneoff'"""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS product_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    product_name TEXT,
                    group_name TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, product_name)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO product_groups (user_id, product_name, group_name)
                VALUES (?, ?, ?)
            """, (user_id, product_name.lower().strip(), group))

    def get_products_by_group(self, user_id: int, group: str = None) -> list:
        with self._conn() as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS product_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        product_name TEXT,
                        group_name TEXT,
                        created_at TEXT DEFAULT (datetime('now')),
                        UNIQUE(user_id, product_name)
                    )
                """)
                if group:
                    rows = conn.execute("""
                        SELECT product_name, group_name FROM product_groups
                        WHERE user_id=? AND group_name=? ORDER BY product_name
                    """, (user_id, group)).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT product_name, group_name FROM product_groups
                        WHERE user_id=? ORDER BY group_name, product_name
                    """, (user_id,)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_product_group(self, user_id: int, product_name: str) -> str | None:
        with self._conn() as conn:
            try:
                row = conn.execute("""
                    SELECT group_name FROM product_groups
                    WHERE user_id=? AND product_name=?
                """, (user_id, product_name.lower().strip())).fetchone()
                return row['group_name'] if row else None
            except Exception:
                return None

    def auto_classify_product(self, user_id: int, product_name: str) -> str | None:
        """Auto-classify based on meal frequency in logs"""
        with self._conn() as conn:
            try:
                row = conn.execute("""
                    SELECT COUNT(*) as cnt FROM meal_logs
                    WHERE user_id=? AND LOWER(description) LIKE ?
                """, (user_id, f'%{product_name.lower()}%')).fetchone()
                count = row['cnt'] if row else 0
                if count >= 10:
                    return 'always'
                elif count >= 3:
                    return 'frequent'
                return None
            except Exception:
                return None

    def remove_product_group(self, user_id: int, product_name: str):
        with self._conn() as conn:
            try:
                conn.execute(
                    "DELETE FROM product_groups WHERE user_id=? AND product_name=?",
                    (user_id, product_name.lower().strip())
                )
            except Exception:
                pass

    # --- Supplements / Medications ---
    def save_supplement(self, user_id: int, name: str, dose: str, timing: str, time_of_day: str = None):
        """timing: before_meal|after_meal|with_meal|independent. time_of_day: 08:00 etc"""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS supplements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    dose TEXT,
                    timing TEXT,
                    time_of_day TEXT,
                    active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                INSERT INTO supplements (user_id, name, dose, timing, time_of_day)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, name, dose, timing, time_of_day))

    def get_supplements(self, user_id: int) -> list:
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT id, name, dose, timing, time_of_day FROM supplements
                    WHERE user_id=? AND active=1 ORDER BY time_of_day
                """, (user_id,)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def log_supplement_taken(self, user_id: int, supplement_id: int):
        from datetime import date
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS supplement_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    supplement_id INTEGER,
                    date TEXT,
                    taken_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, supplement_id, date)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO supplement_logs (user_id, supplement_id, date)
                VALUES (?, ?, ?)
            """, (user_id, supplement_id, date.today().isoformat()))

    def get_supplements_taken_today(self, user_id: int) -> list:
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT supplement_id FROM supplement_logs
                    WHERE user_id=? AND date=?
                """, (user_id, today)).fetchall()
                return [r['supplement_id'] for r in rows]
            except Exception:
                return []

    # --- Tasks ---
    def save_task(self, user_id: int, title: str, time_str: str = None, repeat: str = None):
        """repeat: daily|weekly|none"""
        from datetime import date
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT,
                    time_str TEXT,
                    date TEXT,
                    repeat TEXT DEFAULT 'none',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                INSERT INTO tasks (user_id, title, time_str, date, repeat)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, title, time_str, date.today().isoformat(), repeat or 'none'))

    def get_tasks_today(self, user_id: int) -> list:
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT id, title, time_str, repeat FROM tasks
                    WHERE user_id=? AND (date=? OR repeat IN ('daily'))
                    ORDER BY CASE WHEN time_str IS NULL THEN '99:99' ELSE time_str END
                """, (user_id, today)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def log_task_done(self, user_id: int, task_id: int):
        from datetime import date
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    task_id INTEGER,
                    date TEXT,
                    done_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, task_id, date)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO task_logs (user_id, task_id, date)
                VALUES (?, ?, ?)
            """, (user_id, task_id, date.today().isoformat()))

    def get_tasks_done_today(self, user_id: int) -> list:
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT task_id FROM task_logs WHERE user_id=? AND date=?
                """, (user_id, today)).fetchall()
                return [r['task_id'] for r in rows]
            except Exception:
                return []

    def get_full_day_plan(self, user_id: int) -> dict:
        """Return complete day plan: meals, supplements, tasks, workout"""
        from datetime import date
        today = date.today().isoformat()
        plan = self.get_todays_plan(user_id)
        meals = self.get_today_meals(user_id)
        totals = self.get_today_totals(user_id)
        supplements = self.get_supplements(user_id)
        taken = self.get_supplements_taken_today(user_id)
        tasks = self.get_tasks_today(user_id)
        tasks_done = self.get_tasks_done_today(user_id)
        activity = self.get_today_activity(user_id)
        sleep = self.get_last_sleep(user_id)
        weight = self.get_latest_weight(user_id)
        schedule = self.get_workout_schedule(user_id)

        from datetime import datetime
        day_name = datetime.now().strftime('%A').lower()
        today_workout = schedule.get(day_name) if schedule else None

        return {
            'date': today,
            'plan': plan,
            'meals': meals,
            'totals': totals,
            'supplements': supplements,
            'supplements_taken': taken,
            'tasks': tasks,
            'tasks_done': tasks_done,
            'activity': activity,
            'sleep': sleep,
            'weight': weight,
            'workout': today_workout,
        }

    # --- Weekly template ---
    def save_weekly_template(self, user_id: int, template: dict):
        """Save full weekly meal/task template. template = {monday: {...}, tuesday: {...}, ...}"""
        import json
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS weekly_templates (
                    user_id INTEGER PRIMARY KEY,
                    template_json TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO weekly_templates (user_id, template_json, updated_at)
                VALUES (?, ?, datetime('now'))
            """, (user_id, json.dumps(template, ensure_ascii=False)))

    def get_weekly_template(self, user_id: int) -> dict | None:
        import json
        with self._conn() as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS weekly_templates (
                        user_id INTEGER PRIMARY KEY,
                        template_json TEXT,
                        updated_at TEXT DEFAULT (datetime('now'))
                    )
                """)
                row = conn.execute(
                    "SELECT template_json FROM weekly_templates WHERE user_id=?",
                    (user_id,)
                ).fetchone()
                return json.loads(row['template_json']) if row else None
            except Exception:
                return None

    def save_day_override(self, user_id: int, date_str: str, override: dict):
        """Save today-only override of the weekly template"""
        import json
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS day_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    date TEXT,
                    override_json TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, date)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO day_overrides (user_id, date, override_json)
                VALUES (?, ?, ?)
            """, (user_id, date_str, json.dumps(override, ensure_ascii=False)))

    def get_day_override(self, user_id: int, date_str: str) -> dict | None:
        import json
        with self._conn() as conn:
            try:
                row = conn.execute(
                    "SELECT override_json FROM day_overrides WHERE user_id=? AND date=?",
                    (user_id, date_str)
                ).fetchone()
                return json.loads(row['override_json']) if row else None
            except Exception:
                return None

    def get_effective_day_plan(self, user_id: int) -> dict | None:
        """Returns today override if exists, else weekly template for today"""
        from datetime import date, datetime
        today = date.today().isoformat()
        override = self.get_day_override(user_id, today)
        if override:
            return override
        template = self.get_weekly_template(user_id)
        if not template:
            return None
        days = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
        today_name = days[datetime.now().weekday()]
        return template.get(today_name)

    def get_effective_day_plan_for_date(self, user_id: int, date_str: str) -> dict | None:
        """Returns plan for specific date"""
        from datetime import datetime
        override = self.get_day_override(user_id, date_str)
        if override:
            return override
        template = self.get_weekly_template(user_id)
        if not template:
            return None
        d = datetime.fromisoformat(date_str)
        days = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
        return template.get(days[d.weekday()])

    def save_morning_checkin_done(self, user_id: int, date_str: str):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS morning_checkins (
                    user_id INTEGER,
                    date TEXT,
                    PRIMARY KEY (user_id, date)
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO morning_checkins (user_id, date) VALUES (?, ?)",
                (user_id, date_str)
            )

    def get_morning_checkin_done(self, user_id: int, date_str: str) -> bool:
        with self._conn() as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM morning_checkins WHERE user_id=? AND date=?",
                    (user_id, date_str)
                ).fetchone()
                return bool(row)
            except Exception:
                return False

    # --- Products database (auto-accumulating) ---
    def add_or_update_product(self, user_id: int, product_data: dict):
        """Add product or update. Tracks usage by week, auto-promotes/demotes."""
        from datetime import date
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    name_normalized TEXT,
                    calories_per_100g INTEGER,
                    protein REAL,
                    fat REAL,
                    carbs REAL,
                    standard_portion_g REAL,
                    group_name TEXT DEFAULT 'oneoff',
                    use_count INTEGER DEFAULT 1,
                    first_seen TEXT DEFAULT (datetime('now')),
                    last_seen TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, name_normalized)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS product_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    product_id INTEGER,
                    used_date TEXT,
                    UNIQUE(user_id, product_id, used_date)
                )
            """)
            name = product_data.get('name') or product_data.get('description', '')
            if not name:
                return
            name_norm = name.lower().strip()
            import re
            name_norm = re.sub(r'\s*\d+\s*г\s*$', '', name_norm).strip()

            existing = conn.execute(
                "SELECT id FROM products WHERE user_id=? AND name_normalized=?",
                (user_id, name_norm)
            ).fetchone()

            today = date.today().isoformat()
            if existing:
                pid = existing['id']
                conn.execute(
                    "UPDATE products SET use_count = use_count + 1, last_seen = datetime('now') WHERE id = ?",
                    (pid,)
                )
                conn.execute(
                    "INSERT OR IGNORE INTO product_usage (user_id, product_id, used_date) VALUES (?, ?, ?)",
                    (user_id, pid, today)
                )
            else:
                cur = conn.execute("""
                    INSERT INTO products
                    (user_id, name, name_normalized, calories_per_100g, protein, fat, carbs, standard_portion_g, group_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id, name, name_norm,
                    product_data.get('calories_per_100g') or product_data.get('calories'),
                    product_data.get('protein'), product_data.get('fat'), product_data.get('carbs'),
                    product_data.get('standard_portion_g'),
                    product_data.get('group', 'oneoff')
                ))
                pid = cur.lastrowid
                conn.execute(
                    "INSERT OR IGNORE INTO product_usage (user_id, product_id, used_date) VALUES (?, ?, ?)",
                    (user_id, pid, today)
                )

    def get_product_weekly_stats(self, user_id: int) -> list:
        """For each product: days used in last week, last 30 days. Returns list with promotion candidates."""
        from datetime import date, timedelta
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        month_ago = (date.today() - timedelta(days=30)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT p.id, p.name, p.group_name,
                           SUM(CASE WHEN u.used_date >= ? THEN 1 ELSE 0 END) as week_uses,
                           SUM(CASE WHEN u.used_date >= ? THEN 1 ELSE 0 END) as month_uses,
                           MAX(u.used_date) as last_use
                    FROM products p
                    LEFT JOIN product_usage u ON p.id = u.product_id AND u.user_id = p.user_id
                    WHERE p.user_id = ?
                    GROUP BY p.id
                """, (week_ago, month_ago, user_id)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def find_promotion_candidates(self, user_id: int) -> list:
        """Products that should be promoted from oneoff -> frequent or frequent -> always.
        Returns list ready for AI to ask confirmation."""
        from datetime import date
        today = date.today().isoformat()
        stats = self.get_product_weekly_stats(user_id)
        candidates = []
        for p in stats:
            week_uses = p.get('week_uses', 0) or 0
            month_uses = p.get('month_uses', 0) or 0
            current_group = p.get('group_name', 'oneoff')
            if current_group == 'oneoff' and week_uses >= 3:
                candidates.append({'id': p['id'], 'name': p['name'], 'suggested_group': 'frequent', 'week_uses': week_uses})
            elif current_group == 'frequent' and week_uses >= 5 and month_uses >= 15:
                candidates.append({'id': p['id'], 'name': p['name'], 'suggested_group': 'always', 'week_uses': week_uses, 'month_uses': month_uses})
        return candidates

    def find_demotion_candidates(self, user_id: int) -> list:
        """Products that haven't been used for a month — demote always->frequent or frequent->oneoff"""
        stats = self.get_product_weekly_stats(user_id)
        candidates = []
        for p in stats:
            month_uses = p.get('month_uses', 0) or 0
            current_group = p.get('group_name', 'oneoff')
            if current_group != 'oneoff' and month_uses == 0:
                new_group = 'frequent' if current_group == 'always' else 'oneoff'
                candidates.append({'id': p['id'], 'name': p['name'], 'current_group': current_group, 'suggested_group': new_group})
        return candidates


    def upsert_product_with_per100g(self, user_id: int, p: dict):
        """Save/update product with per-100g KBJU and standard portion (median-based)."""
        with self._conn() as conn:
            # Ensure new columns exist
            for col, ctype in [
                ('protein_per_100g', 'REAL'),
                ('fat_per_100g', 'REAL'),
                ('carbs_per_100g', 'REAL'),
            ]:
                try:
                    conn.execute(f"ALTER TABLE products ADD COLUMN {col} {ctype}")
                except Exception:
                    pass

            existing = conn.execute(
                "SELECT id, use_count FROM products WHERE user_id=? AND name_normalized=?",
                (user_id, p['name_norm'])
            ).fetchone()

            if existing:
                # Update median values, increment use_count
                new_count = (existing['use_count'] or 0) + p.get('use_count', 1)
                conn.execute("""
                    UPDATE products SET
                        calories_per_100g = ?,
                        protein_per_100g = ?,
                        fat_per_100g = ?,
                        carbs_per_100g = ?,
                        standard_portion_g = ?,
                        use_count = ?,
                        last_seen = datetime('now')
                    WHERE id = ?
                """, (
                    p['calories_per_100g'], p['protein_per_100g'],
                    p['fat_per_100g'], p['carbs_per_100g'],
                    p.get('standard_portion_g'), new_count,
                    existing['id']
                ))
            else:
                conn.execute("""
                    INSERT INTO products
                    (user_id, name, name_normalized,
                     calories_per_100g, protein_per_100g, fat_per_100g, carbs_per_100g,
                     standard_portion_g, group_name, use_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id, p['name'], p['name_norm'],
                    p['calories_per_100g'], p['protein_per_100g'],
                    p['fat_per_100g'], p['carbs_per_100g'],
                    p.get('standard_portion_g'),
                    'oneoff', p.get('use_count', 1)
                ))

    def mark_product_group(self, user_id: int, name: str, group: str):
        with self._conn() as conn:
            try:
                name_norm = name.lower().strip()
                conn.execute(
                    "UPDATE products SET group_name=? WHERE user_id=? AND name_normalized=?",
                    (group, user_id, name_norm)
                )
            except Exception:
                pass

    def get_products_summary(self, user_id: int) -> dict:
        """Get top products grouped by category for AI context"""
        with self._conn() as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS products (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER, name TEXT, name_normalized TEXT,
                        calories_per_100g INTEGER, protein REAL, fat REAL, carbs REAL,
                        standard_portion_g REAL, group_name TEXT DEFAULT 'oneoff',
                        use_count INTEGER DEFAULT 1,
                        first_seen TEXT, last_seen TEXT
                    )
                """)
                rows = conn.execute("""
                    SELECT name, group_name, use_count, calories_per_100g, protein, fat, carbs, standard_portion_g
                    FROM products WHERE user_id=?
                    ORDER BY use_count DESC LIMIT 50
                """, (user_id,)).fetchall()
            except Exception:
                return {}

        result = {'always': [], 'frequent': [], 'oneoff': []}
        for r in rows:
            d = dict(r)
            grp = d.get('group_name', 'oneoff')
            if grp in result and len(result[grp]) < 20:
                result[grp].append({
                    'name': d['name'],
                    'count': d['use_count'],
                    'kcal_100g': d.get('calories_per_100g'),
                    'portion_g': d.get('standard_portion_g'),
                })
        return result

    def clear_today_meals(self, user_id: int):
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM meal_logs WHERE user_id=? AND date=?", (user_id, today))

    def cleanup_old_overrides(self, user_id: int):
        """Remove day_overrides where date is in the past (before this Monday).
        Override стирается когда наступает понедельник после своей даты."""
        from datetime import date, timedelta
        today = date.today()
        # This Monday (or today if today is Monday)
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        cutoff = this_monday.isoformat()
        with self._conn() as conn:
            try:
                conn.execute(
                    "DELETE FROM day_overrides WHERE user_id=? AND date < ?",
                    (user_id, cutoff)
                )
            except Exception:
                pass

    def get_future_overrides(self, user_id: int, days_ahead: int = 7) -> list:
        """Get all day overrides for next N days"""
        from datetime import date, timedelta
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=days_ahead)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT date, override_json FROM day_overrides
                    WHERE user_id=? AND date >= ? AND date <= ?
                    ORDER BY date
                """, (user_id, today, end)).fetchall()
                import json
                return [
                    {"date": r["date"], "plan": json.loads(r["override_json"])}
                    for r in rows
                ]
            except Exception:
                return []

    def log_meal_for_date(self, user_id: int, meal: dict, date_str: str):
        """Log meal for specific date with optional portion_g and product_name"""
        with self._conn() as conn:
            # Add new columns if missing
            for col, ctype in [('portion_g', 'REAL'), ('product_name', 'TEXT')]:
                try:
                    conn.execute(f"ALTER TABLE meal_logs ADD COLUMN {col} {ctype}")
                except Exception:
                    pass

            conn.execute("""
                INSERT INTO meal_logs
                (user_id, date, time, description, calories, protein, fat, carbs, portion_g, product_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, date_str,
                meal.get('time', '12:00'),
                meal.get('description', ''),
                meal.get('calories', 0),
                meal.get('protein', 0),
                meal.get('fat', 0),
                meal.get('carbs', 0),
                meal.get('portion_g'),
                meal.get('product_name'),
            ))

    def log_workout_for_date(self, user_id: int, workout: dict, date_str: str):
        import json
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO workout_logs (user_id, date, time, workout_type, summary, exercises_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id, date_str,
                workout.get('time', ''),
                workout.get('type', ''),
                workout.get('summary', f"{workout.get('type','')} {workout.get('duration_min',0)} мин"),
                json.dumps(workout.get('exercises', []), ensure_ascii=False)
            ))

    def get_import_summary(self, user_id: int) -> dict:
        """Summary of imported data for AI analysis"""
        with self._conn() as conn:
            try:
                meals_count = conn.execute(
                    "SELECT COUNT(DISTINCT date) FROM meal_logs WHERE user_id=?",
                    (user_id,)
                ).fetchone()[0]
                weight_count = conn.execute(
                    "SELECT COUNT(*) FROM weight_logs WHERE user_id=?",
                    (user_id,)
                ).fetchone()[0]
                products_count = conn.execute(
                    "SELECT COUNT(*) FROM products WHERE user_id=?",
                    (user_id,)
                ).fetchone()[0]

                # Date range
                first_meal = conn.execute(
                    "SELECT MIN(date), MAX(date) FROM meal_logs WHERE user_id=?",
                    (user_id,)
                ).fetchone()
                first_weight = conn.execute(
                    "SELECT MIN(date), MAX(date) FROM weight_logs WHERE user_id=?",
                    (user_id,)
                ).fetchone()

                return {
                    "meal_days": meals_count,
                    "weight_records": weight_count,
                    "products_in_base": products_count,
                    "meal_range": list(first_meal) if first_meal else None,
                    "weight_range": list(first_weight) if first_weight else None,
                }
            except Exception:
                return {}

    def get_monthly_aggregates(self, user_id: int, months: int = 12) -> list:
        """For each month: avg weight, avg calories, days with data"""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=months * 31)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT
                        substr(m.date, 1, 7) as month,
                        AVG(m.calories) as avg_cal,
                        AVG(m.protein) as avg_prot,
                        COUNT(DISTINCT m.date) as days,
                        (SELECT AVG(weight) FROM weight_logs w
                         WHERE w.user_id = m.user_id
                         AND substr(w.date, 1, 7) = substr(m.date, 1, 7)) as avg_weight
                    FROM (
                        SELECT user_id, date, SUM(calories) as calories, SUM(protein) as protein
                        FROM meal_logs WHERE user_id = ? AND date >= ?
                        GROUP BY user_id, date
                    ) m
                    GROUP BY month
                    ORDER BY month
                """, (user_id, cutoff)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_meals_recent_detailed(self, user_id: int, days: int = 7) -> list:
        """Last N days - all meals with full detail"""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT date, time, description, calories, protein, fat, carbs, meal_type
                    FROM meal_logs WHERE user_id=? AND date >= ?
                    ORDER BY date DESC, time
                """, (user_id, cutoff)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                # Try without meal_type column
                try:
                    rows = conn.execute("""
                        SELECT date, time, description, calories, protein, fat, carbs
                        FROM meal_logs WHERE user_id=? AND date >= ?
                        ORDER BY date DESC, time
                    """, (user_id, cutoff)).fetchall()
                    return [dict(r) for r in rows]
                except Exception:
                    return []

    def get_meals_daily_totals(self, user_id: int, days_from: int, days_to: int) -> list:
        """Days from-to: only daily totals"""
        from datetime import date, timedelta
        cutoff_start = (date.today() - timedelta(days=days_to)).isoformat()
        cutoff_end = (date.today() - timedelta(days=days_from)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT date,
                           SUM(calories) as calories,
                           SUM(protein) as protein,
                           SUM(fat) as fat,
                           SUM(carbs) as carbs
                    FROM meal_logs
                    WHERE user_id=? AND date >= ? AND date <= ?
                    GROUP BY date
                    ORDER BY date DESC
                """, (user_id, cutoff_start, cutoff_end)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_weight_smart(self, user_id: int) -> list:
        """Returns weight history with smart sampling:
        - Last 14 days: all measurements
        - 15-30 days: every other day
        - 31-60 days: weekly
        - 60+ days: monthly
        """
        from datetime import date, timedelta
        today = date.today()
        cutoff_14 = (today - timedelta(days=14)).isoformat()
        cutoff_30 = (today - timedelta(days=30)).isoformat()
        cutoff_60 = (today - timedelta(days=60)).isoformat()

        result = []
        with self._conn() as conn:
            try:
                # Last 14 days - all
                rows = conn.execute("""
                    SELECT date, weight FROM weight_logs
                    WHERE user_id=? AND date >= ?
                    ORDER BY date DESC
                """, (user_id, cutoff_14)).fetchall()
                result.extend([dict(r) for r in rows])

                # 15-30 days: every other day
                rows = conn.execute("""
                    SELECT date, weight FROM weight_logs
                    WHERE user_id=? AND date >= ? AND date < ?
                    ORDER BY date DESC
                """, (user_id, cutoff_30, cutoff_14)).fetchall()
                for i, r in enumerate(rows):
                    if i % 2 == 0:
                        result.append(dict(r))

                # 31-60 days: weekly (last day of each week)
                rows = conn.execute("""
                    SELECT date, weight FROM weight_logs
                    WHERE user_id=? AND date >= ? AND date < ?
                    ORDER BY date DESC
                """, (user_id, cutoff_60, cutoff_30)).fetchall()
                seen_weeks = set()
                for r in rows:
                    week = r['date'][:7] + '-W' + str(int(r['date'][8:10]) // 7)
                    if week not in seen_weeks:
                        seen_weeks.add(week)
                        result.append(dict(r))

                # 60+ days: monthly (last day of each month)
                rows = conn.execute("""
                    SELECT date, weight, substr(date, 1, 7) as month FROM weight_logs
                    WHERE user_id=? AND date < ?
                    ORDER BY date DESC
                """, (user_id, cutoff_60)).fetchall()
                seen_months = set()
                for r in rows:
                    if r['month'] not in seen_months:
                        seen_months.add(r['month'])
                        d = dict(r)
                        d.pop('month', None)
                        result.append(d)
            except Exception as e:
                pass

        return result

    def log_meal_with_meal_type(self, user_id: int, meal: dict, date_str: str = None, time_str: str = None):
        """Log meal with meal_type field (breakfast/lunch/dinner/snack/etc)"""
        from datetime import date, datetime
        with self._conn() as conn:
            for col, ctype in [('meal_type', 'TEXT'), ('portion_g', 'REAL'), ('product_name', 'TEXT')]:
                try:
                    conn.execute(f"ALTER TABLE meal_logs ADD COLUMN {col} {ctype}")
                except Exception:
                    pass

            # Auto-detect meal_type from time if not provided
            mt = meal.get('meal_type')
            t = time_str or meal.get('time') or datetime.now().strftime('%H:%M')
            if not mt:
                hour = int(t[:2]) if len(t) >= 2 and t[:2].isdigit() else 12
                if 5 <= hour < 11:
                    mt = 'breakfast'
                elif 11 <= hour < 15:
                    mt = 'lunch'
                elif 15 <= hour < 18:
                    mt = 'snack'
                elif 18 <= hour < 22:
                    mt = 'dinner'
                else:
                    mt = 'late'

            conn.execute("""
                INSERT INTO meal_logs
                (user_id, date, time, description, calories, protein, fat, carbs,
                 meal_type, portion_g, product_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                date_str or date.today().isoformat(),
                t,
                meal.get('description', ''),
                meal.get('calories', 0),
                meal.get('protein', 0),
                meal.get('fat', 0),
                meal.get('carbs', 0),
                mt,
                meal.get('portion_g'),
                meal.get('product_name'),
            ))

    def get_user_timezone(self, user_id: int) -> str:
        with self._conn() as conn:
            try:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN timezone TEXT DEFAULT 'Europe/Moscow'")
            except Exception:
                pass
            try:
                row = conn.execute(
                    "SELECT timezone FROM user_profiles WHERE user_id=?",
                    (user_id,)
                ).fetchone()
                return row['timezone'] if row and row['timezone'] else 'Europe/Moscow'
            except Exception:
                return 'Europe/Moscow'

    def set_user_timezone(self, user_id: int, tz: str):
        with self._conn() as conn:
            try:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN timezone TEXT DEFAULT 'Europe/Moscow'")
            except Exception:
                pass
            conn.execute(
                "UPDATE user_profiles SET timezone=? WHERE user_id=?",
                (tz, user_id)
            )

    def get_last_message_time(self, user_id: int) -> str:
        """For lazy daily summary - check if user was active recently"""
        with self._conn() as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_activity (
                        user_id INTEGER PRIMARY KEY,
                        last_message_at TEXT
                    )
                """)
                row = conn.execute(
                    "SELECT last_message_at FROM user_activity WHERE user_id=?",
                    (user_id,)
                ).fetchone()
                return row['last_message_at'] if row else None
            except Exception:
                return None

    def update_last_message_time(self, user_id: int):
        from datetime import datetime
        with self._conn() as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_activity (
                        user_id INTEGER PRIMARY KEY,
                        last_message_at TEXT
                    )
                """)
                conn.execute(
                    "INSERT OR REPLACE INTO user_activity (user_id, last_message_at) VALUES (?, ?)",
                    (user_id, datetime.now().isoformat())
                )
            except Exception:
                pass

    def get_summary_done_today(self, user_id: int, kind: str = 'evening') -> bool:
        from datetime import date
        with self._conn() as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_summary_log (
                        user_id INTEGER, date TEXT, kind TEXT,
                        PRIMARY KEY (user_id, date, kind)
                    )
                """)
                row = conn.execute(
                    "SELECT 1 FROM daily_summary_log WHERE user_id=? AND date=? AND kind=?",
                    (user_id, date.today().isoformat(), kind)
                ).fetchone()
                return bool(row)
            except Exception:
                return False

    def mark_summary_done(self, user_id: int, kind: str = 'evening'):
        from datetime import date
        with self._conn() as conn:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_summary_log (
                        user_id INTEGER, date TEXT, kind TEXT,
                        PRIMARY KEY (user_id, date, kind)
                    )
                """)
                conn.execute(
                    "INSERT OR REPLACE INTO daily_summary_log (user_id, date, kind) VALUES (?, ?, ?)",
                    (user_id, date.today().isoformat(), kind)
                )
            except Exception:
                pass

    def get_meal_type_portion(self, user_id: int, product_name: str, meal_type: str) -> float:
        """Get standard portion for product+meal_type combination based on history"""
        from statistics import median
        with self._conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT portion_g FROM meal_logs
                    WHERE user_id=? AND product_name=? AND meal_type=? AND portion_g IS NOT NULL
                    ORDER BY date DESC LIMIT 30
                """, (user_id, product_name, meal_type)).fetchall()
                portions = [r['portion_g'] for r in rows if r['portion_g']]
                if not portions:
                    return None
                med = median(portions)
                # Filter outliers (0.5x to 2x of median)
                clean = [p for p in portions if 0.5 * med <= p <= 2.0 * med]
                return median(clean) if clean else med
            except Exception:
                return None

