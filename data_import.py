"""
Import module - parses Apple Health XML and FatSecret CSV exports.
Saves to existing database, no AI calls during parsing.
"""
import logging
import zipfile
import io
import csv
import re
from datetime import datetime, date, timedelta
from xml.etree import ElementTree as ET
from collections import defaultdict
from database import Database

logger = logging.getLogger(__name__)

# Filter thresholds for FatSecret data
MIN_CALORIES_PER_DAY = 1700
MAX_CALORIES_PER_DAY = 2700


def import_apple_health_zip(user_id: int, zip_bytes: bytes, db: Database) -> dict:
    """
    Parse Apple Health export.zip — extracts:
    - Weight (HKQuantityTypeIdentifierBodyMass)
    - Steps (HKQuantityTypeIdentifierStepCount)
    - Active calories (HKQuantityTypeIdentifierActiveEnergyBurned)
    - Sleep (HKCategoryTypeIdentifierSleepAnalysis)
    - Body fat % (HKQuantityTypeIdentifierBodyFatPercentage)
    - Lean body mass (HKQuantityTypeIdentifierLeanBodyMass)
    - Workouts
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # Apple Health stores main data in apple_health_export/export.xml
            xml_name = None
            for name in zf.namelist():
                if name.endswith("export.xml") and "apple_health_export" in name:
                    xml_name = name
                    break
            if not xml_name:
                # Try root export.xml
                for name in zf.namelist():
                    if name.endswith("export.xml"):
                        xml_name = name
                        break
            if not xml_name:
                return {"error": "Не нашёл export.xml в архиве"}

            xml_data = zf.read(xml_name)
    except zipfile.BadZipFile:
        # Maybe it's already XML
        xml_data = zip_bytes

    return _parse_apple_health_xml(user_id, xml_data, db)


def _parse_apple_health_xml(user_id: int, xml_data: bytes, db: Database) -> dict:
    """Parse Apple Health XML using iterparse for memory efficiency"""
    stats = {
        "weight": 0, "steps": 0, "calories": 0, "sleep": 0,
        "fat_percent": 0, "workouts": 0, "errors": 0
    }

    # Aggregate by date
    by_date = defaultdict(lambda: {
        "weight": [], "steps": 0, "active_cal": 0,
        "sleep_minutes": 0, "fat_percent": [], "lean_mass": []
    })
    workouts_list = []

    try:
        # Use iterparse to handle large files
        for event, elem in ET.iterparse(io.BytesIO(xml_data), events=('end',)):
            if elem.tag == 'Record':
                rec_type = elem.get('type', '')
                start = elem.get('startDate', '')
                end = elem.get('endDate', '')
                value = elem.get('value', '0')

                try:
                    if not start:
                        elem.clear()
                        continue
                    d = start[:10]  # YYYY-MM-DD

                    if rec_type == 'HKQuantityTypeIdentifierBodyMass':
                        by_date[d]['weight'].append(float(value))
                        stats['weight'] += 1
                    elif rec_type == 'HKQuantityTypeIdentifierStepCount':
                        by_date[d]['steps'] += int(float(value))
                        stats['steps'] += 1
                    elif rec_type == 'HKQuantityTypeIdentifierActiveEnergyBurned':
                        by_date[d]['active_cal'] += float(value)
                        stats['calories'] += 1
                    elif rec_type == 'HKQuantityTypeIdentifierBodyFatPercentage':
                        by_date[d]['fat_percent'].append(float(value) * 100)  # 0.2 -> 20%
                        stats['fat_percent'] += 1
                    elif rec_type == 'HKQuantityTypeIdentifierLeanBodyMass':
                        by_date[d]['lean_mass'].append(float(value))
                    elif rec_type == 'HKCategoryTypeIdentifierSleepAnalysis':
                        if 'Asleep' in elem.get('value', '') or 'InBed' in elem.get('value', ''):
                            try:
                                start_dt = datetime.fromisoformat(start.replace(' +0000', '+00:00').replace(' ', 'T', 1)[:19])
                                end_dt = datetime.fromisoformat(end.replace(' +0000', '+00:00').replace(' ', 'T', 1)[:19])
                                minutes = (end_dt - start_dt).total_seconds() / 60
                                by_date[d]['sleep_minutes'] += minutes
                                stats['sleep'] += 1
                            except Exception:
                                pass
                except Exception:
                    stats['errors'] += 1
                elem.clear()
            elif elem.tag == 'Workout':
                try:
                    start = elem.get('startDate', '')
                    duration = float(elem.get('duration', 0))
                    cal = float(elem.get('totalEnergyBurned', 0))
                    wtype = elem.get('workoutActivityType', '').replace('HKWorkoutActivityType', '')
                    if start:
                        workouts_list.append({
                            'date': start[:10],
                            'time': start[11:16] if len(start) > 16 else '',
                            'type': wtype,
                            'duration_min': round(duration),
                            'calories': round(cal),
                        })
                        stats['workouts'] += 1
                except Exception:
                    stats['errors'] += 1
                elem.clear()
    except Exception as e:
        logger.error(f"XML parse error: {e}")
        return {"error": f"Ошибка парсинга XML: {e}"}

    # Save to database
    saved = {"weight": 0, "activity": 0, "sleep": 0, "workouts": 0}

    for d, data in by_date.items():
        # Weight - take median for the day
        if data['weight']:
            sorted_w = sorted(data['weight'])
            median_w = sorted_w[len(sorted_w) // 2]
            try:
                db.log_weight_for_date(user_id, round(median_w, 1), d)
                saved['weight'] += 1
            except Exception:
                pass

        # Activity - daily totals
        if data['steps'] > 0 or data['active_cal'] > 0:
            try:
                db.log_activity_for_date(user_id, {
                    'steps': data['steps'],
                    'calories_burned': round(data['active_cal']),
                    'source': 'apple_health_import',
                }, d)
                saved['activity'] += 1
            except Exception:
                pass

        # Sleep
        if data['sleep_minutes'] > 0:
            try:
                hours = round(data['sleep_minutes'] / 60, 1)
                db.log_sleep(user_id, hours, None, d)
                saved['sleep'] += 1
            except Exception:
                pass

        # Fat % from Apple Health (saves as inbody-style record)
        if data['fat_percent']:
            avg_fat = sum(data['fat_percent']) / len(data['fat_percent'])
            avg_lean = sum(data['lean_mass']) / len(data['lean_mass']) if data['lean_mass'] else None
            try:
                # Use weight from same day if available
                weight = data['weight'][0] if data['weight'] else None
                inbody_data = {
                    'date': d,
                    'weight': round(weight, 1) if weight else None,
                    'fat_percent': round(avg_fat, 1),
                    'muscle_mass': round(avg_lean, 1) if avg_lean else None,
                }
                # Save to body_composition_logs (separate from clinical InBody)
                _save_body_composition(db, user_id, inbody_data)
            except Exception:
                pass

    # Workouts
    for w in workouts_list:
        try:
            db.log_workout_for_date(user_id, w, w['date'])
            saved['workouts'] += 1
        except Exception:
            pass

    return {
        "parsed": stats,
        "saved": saved,
        "days_with_data": len(by_date),
        "date_range": _get_date_range(by_date.keys()) if by_date else None,
    }


def _get_date_range(dates) -> dict:
    if not dates:
        return None
    sorted_d = sorted(dates)
    return {"from": sorted_d[0], "to": sorted_d[-1]}


def _save_body_composition(db: Database, user_id: int, data: dict):
    """Save body composition data (from PICOOC/Apple Health) - separate from clinical InBody"""
    import json
    with db._conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS body_composition_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date TEXT,
                weight REAL,
                fat_percent REAL,
                muscle_mass REAL,
                source TEXT DEFAULT 'apple_health',
                raw_json TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, date, source)
            )
        """)
        conn.execute("""
            INSERT OR REPLACE INTO body_composition_logs
            (user_id, date, weight, fat_percent, muscle_mass, source, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, data.get('date'),
            data.get('weight'), data.get('fat_percent'),
            data.get('muscle_mass'), data.get('source', 'apple_health'),
            json.dumps(data, ensure_ascii=False)
        ))


def import_fatsecret_csv(user_id: int, csv_bytes: bytes, db: Database, months_limit: int = 12) -> dict:
    """
    Parse FatSecret CSV export.
    Filters: only days within MIN_CALORIES_PER_DAY..MAX_CALORIES_PER_DAY range.
    Imports only last N months.
    """
    try:
        # Try multiple encodings
        text = None
        for enc in ['utf-8', 'utf-8-sig', 'cp1251', 'windows-1251']:
            try:
                text = csv_bytes.decode(enc)
                break
            except Exception:
                continue
        if not text:
            return {"error": "Не смог декодировать CSV"}

        # FatSecret CSV columns vary - try to detect
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return {"error": "CSV пустой"}

        # Common FatSecret columns: Date, Meal, Food, Servings, Calories, Protein, Fat, Carbs
        # Detect date column
        first = rows[0]
        date_col = None
        for col in first.keys():
            if col and ('date' in col.lower() or 'дата' in col.lower()):
                date_col = col
                break
        if not date_col:
            date_col = list(first.keys())[0]  # fallback

        # Filter by date range (last N months)
        cutoff = date.today() - timedelta(days=30 * months_limit)
        cutoff_str = cutoff.isoformat()

        # Group by date to compute totals for filtering
        by_date = defaultdict(list)
        for row in rows:
            try:
                d_str = row.get(date_col, '').strip()
                if not d_str:
                    continue
                d = _parse_fatsecret_date(d_str)
                if not d or d < cutoff_str:
                    continue
                by_date[d].append(row)
            except Exception:
                continue

        # Compute daily totals and filter
        valid_days = {}
        skipped_days = 0
        for d, day_rows in by_date.items():
            total_cal = 0
            for r in day_rows:
                total_cal += _safe_float(r, ['Calories', 'calories', 'Калории', 'ккал'])
            if MIN_CALORIES_PER_DAY <= total_cal <= MAX_CALORIES_PER_DAY:
                valid_days[d] = day_rows
            else:
                skipped_days += 1

        # Save valid days
        saved_meals = 0
        saved_products = 0
        seen_products = set()

        for d, day_rows in valid_days.items():
            for r in day_rows:
                food = r.get('Food', '') or r.get('Food Item', '') or r.get('Продукт', '') or r.get('Описание', '')
                meal_type = r.get('Meal', '') or r.get('Тип', '') or 'Приём пищи'
                cal = _safe_float(r, ['Calories', 'calories', 'Калории'])
                prot = _safe_float(r, ['Protein (g)', 'Protein', 'Белки', 'Белок'])
                fat = _safe_float(r, ['Fat (g)', 'Fat', 'Жиры'])
                carbs = _safe_float(r, ['Carbohydrate (g)', 'Carbs', 'Carbohydrate', 'Углеводы'])
                portion = r.get('Servings', '') or r.get('Порция', '')

                if not food or cal == 0:
                    continue

                desc = f"{food}"
                if portion:
                    desc += f" ({portion})"

                meal = {
                    'description': desc,
                    'calories': round(cal),
                    'protein': round(prot, 1),
                    'fat': round(fat, 1),
                    'carbs': round(carbs, 1),
                    'time': _get_meal_time(meal_type),
                }

                try:
                    db.log_meal_for_date(user_id, meal, d)
                    saved_meals += 1
                except Exception as e:
                    logger.error(f"Save meal error: {e}")

                # Add unique products to base
                food_key = food.lower().strip()
                if food_key not in seen_products:
                    seen_products.add(food_key)
                    try:
                        db.add_or_update_product(user_id, {
                            'name': food,
                            'calories': round(cal),
                            'protein': round(prot, 1),
                            'fat': round(fat, 1),
                            'carbs': round(carbs, 1),
                        })
                        saved_products += 1
                    except Exception:
                        pass

        return {
            "parsed_rows": len(rows),
            "valid_days": len(valid_days),
            "skipped_days_out_of_range": skipped_days,
            "saved_meals": saved_meals,
            "saved_products": saved_products,
        }

    except Exception as e:
        logger.error(f"FatSecret import error: {e}")
        return {"error": str(e)}


def _safe_float(row: dict, keys: list) -> float:
    for k in keys:
        val = row.get(k)
        if val is None:
            continue
        try:
            cleaned = str(val).replace(',', '.').strip()
            cleaned = re.sub(r'[^\d.\-]', '', cleaned)
            if cleaned:
                return float(cleaned)
        except Exception:
            continue
    return 0.0


def _parse_fatsecret_date(s: str) -> str:
    """Try multiple date formats, return YYYY-MM-DD"""
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except Exception:
            continue
    return None


def _get_meal_time(meal_type: str) -> str:
    """Map meal type to typical time"""
    mt = meal_type.lower()
    if 'breakfast' in mt or 'завтрак' in mt:
        return '09:00'
    elif 'lunch' in mt or 'обед' in mt:
        return '13:00'
    elif 'dinner' in mt or 'ужин' in mt:
        return '19:00'
    elif 'snack' in mt or 'перекус' in mt:
        return '16:00'
    return '12:00'
