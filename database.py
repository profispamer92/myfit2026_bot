import sqlite3
import json
from datetime import datetime, date, timedelta
from typing import Optional


class Database:
    def __init__(self, db_path: str = "fitness.db"):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
                INSERT OR REPLACE INTO user_profiles
                (user_id, weight, height, age, activity, goal, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """, (user_id, profile.get('weight'), profile.get('height'),
                  profile.get('age'), profile.get('activity'), profile.get('goal')))

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
            rows = conn.execute("""\n"\n"
                SELECT date, weight FROM weight_logs
                WHERE user_id=? AND date>=?
                ORDER BY date DESC
            """, (user_id, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def save_pending_plan(self, user_id: int, new_plan: dict | None, reason: str):
        import json
        with self._conn() as conn:
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
                INSERT INTO weight_logs (user_id, date, weight)
                VALUES (?, ?, ?)
            """, (user_id, today, weight))

    def get_today_totals(self, user_id: int) -> Optional[dict]:
        today = date.today().isoformat()
        with self._conn() as conn:
            row = conn.execute("""\n"\n"
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
            rows = conn.execute("""\n"\n"
                SELECT time, description, calories, protein, fat, carbs
                FROM meal_logs WHERE user_id = ? AND date = ?
                ORDER BY time
            """, (user_id, today)).fetchall()
            return [dict(r) for r in rows]

    def get_week_stats(self, user_id: int) -> list:
        today = date.today()
        week_ago = (today - timedelta(days=6)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""\n"\n"
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
            meals = conn.execute("""\n"\n"
                SELECT date, time, description, calories, protein, fat, carbs
                FROM meal_logs WHERE user_id = ? AND date >= ?
                ORDER BY date DESC, time DESC LIMIT 20
            """, (user_id, cutoff)).fetchall()

            workouts = conn.execute("""\n"\n"
                SELECT date, time, workout_type, summary
                FROM workout_logs WHERE user_id = ? AND date >= ?
                ORDER BY date DESC LIMIT 10
            """, (user_id, cutoff)).fetchall()

            weights = conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            rows = conn.execute("""\n"\n"
                SELECT description, calories, protein, fat, carbs, date, time
                FROM meal_logs
                WHERE user_id=? AND date>=?
                ORDER BY date DESC
            """, (user_id, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def log_activity_for_date(self, user_id: int, data: dict, for_date: str):
        """Log activity for a specific date (used for yesterday's steps sync)"""
        with self._conn() as conn:
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            rows = conn.execute("""\n"\n"
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
                rows = conn.execute("""\n"\n"
                    SELECT date, steps, calories_burned, active_minutes
                    FROM activity_logs WHERE user_id=? AND date>=?
                    ORDER BY date DESC
                """, (user_id, cutoff)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_latest_weight(self, user_id: int) -> float | None:
        with self._conn() as conn:
            row = conn.execute("""\n"\n"
                SELECT weight FROM weight_logs WHERE user_id=?
                ORDER BY date DESC LIMIT 1
            """, (user_id,)).fetchone()
            return row['weight'] if row else None

    def save_inbody(self, user_id: int, data: dict):
        """Save InBody measurement"""
        from datetime import date
        import json
        with self._conn() as conn:
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
                row = conn.execute("""\n"\n"
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
                rows = conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
                INSERT OR REPLACE INTO sleep_logs (user_id, date, hours, quality, source)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, target_date, hours, quality, 'shortcuts' if date_str else 'manual'))

    def get_sleep_history(self, user_id: int, days: int = 30) -> list:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""\n"\n"
                    SELECT date, hours, quality, source FROM sleep_logs
                    WHERE user_id=? AND date>=? ORDER BY date DESC
                """, (user_id, cutoff)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_last_sleep(self, user_id: int) -> dict | None:
        with self._conn() as conn:
            try:
                row = conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
                CREATE TABLE IF NOT EXISTS product_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    product_name TEXT,
                    group_name TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, product_name)
                )
            """)
            conn.execute("""\n"\n"
                INSERT OR REPLACE INTO product_groups (user_id, product_name, group_name)
                VALUES (?, ?, ?)
            """, (user_id, product_name.lower().strip(), group))

    def get_products_by_group(self, user_id: int, group: str = None) -> list:
        with self._conn() as conn:
            try:
                conn.execute("""\n"\n"
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
                    rows = conn.execute("""\n"\n"
                        SELECT product_name, group_name FROM product_groups
                        WHERE user_id=? AND group_name=? ORDER BY product_name
                    """, (user_id, group)).fetchall()
                else:
                    rows = conn.execute("""\n"\n"
                        SELECT product_name, group_name FROM product_groups
                        WHERE user_id=? ORDER BY group_name, product_name
                    """, (user_id,)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def get_product_group(self, user_id: int, product_name: str) -> str | None:
        with self._conn() as conn:
            try:
                row = conn.execute("""\n"\n"
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
                row = conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
                INSERT INTO supplements (user_id, name, dose, timing, time_of_day)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, name, dose, timing, time_of_day))

    def get_supplements(self, user_id: int) -> list:
        with self._conn() as conn:
            try:
                rows = conn.execute("""\n"\n"
                    SELECT id, name, dose, timing, time_of_day FROM supplements
                    WHERE user_id=? AND active=1 ORDER BY time_of_day
                """, (user_id,)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def log_supplement_taken(self, user_id: int, supplement_id: int):
        from datetime import date
        with self._conn() as conn:
            conn.execute("""\n"\n"
                CREATE TABLE IF NOT EXISTS supplement_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    supplement_id INTEGER,
                    date TEXT,
                    taken_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, supplement_id, date)
                )
            """)
            conn.execute("""\n"\n"
                INSERT OR REPLACE INTO supplement_logs (user_id, supplement_id, date)
                VALUES (?, ?, ?)
            """, (user_id, supplement_id, date.today().isoformat()))

    def get_supplements_taken_today(self, user_id: int) -> list:
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
                INSERT INTO tasks (user_id, title, time_str, date, repeat)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, title, time_str, date.today().isoformat(), repeat or 'none'))

    def get_tasks_today(self, user_id: int) -> list:
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""\n"\n"
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
            conn.execute("""\n"\n"
                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    task_id INTEGER,
                    date TEXT,
                    done_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, task_id, date)
                )
            """)
            conn.execute("""\n"\n"
                INSERT OR REPLACE INTO task_logs (user_id, task_id, date)
                VALUES (?, ?, ?)
            """, (user_id, task_id, date.today().isoformat()))

    def get_tasks_done_today(self, user_id: int) -> list:
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            try:
                rows = conn.execute("""\n"\n"
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

