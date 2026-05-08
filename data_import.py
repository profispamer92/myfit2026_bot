"""
Import module - Apple Health XML and FatSecret CSV.
Two-stage import:
1. Parse and detect anomalies
2. Show user, wait for confirmation
3. Save valid + optionally remove anomalies
"""
import logging
import zipfile
import io
import csv
import re
import json
from datetime import datetime, date, timedelta
from xml.etree import ElementTree as ET
from collections import defaultdict
from database import Database

logger = logging.getLogger(__name__)

MIN_CALORIES_PER_DAY = 1700
MAX_CALORIES_PER_DAY = 2700

# Anomaly thresholds
W_MIN, W_MAX = 30, 250  # weight kg
STEPS_MAX = 100000
SLEEP_MIN_H, SLEEP_MAX_H = 1, 16
FAT_MIN, FAT_MAX = 3, 60  # body fat %
HR_MIN, HR_MAX = 30, 120


def parse_apple_health_zip(zip_bytes: bytes) -> dict:
    """Parse Apple Health export.zip - returns parsed data + anomalies for confirmation"""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            xml_name = None
            for name in zf.namelist():
                if name.endswith("export.xml") and "apple_health_export" in name:
                    xml_name = name
                    break
            if not xml_name:
                for name in zf.namelist():
                    if name.endswith("export.xml"):
                        xml_name = name
                        break
            if not xml_name:
                return {"error": "Не нашёл export.xml в архиве"}
            xml_data = zf.read(xml_name)
    except zipfile.BadZipFile:
        xml_data = zip_bytes

    return _parse_apple_health_xml(xml_data)


def _parse_apple_health_xml(xml_data: bytes) -> dict:
    """Parse XML, return data grouped by date + anomalies list"""
    by_date = defaultdict(lambda: {
        "weight": [], "steps": 0, "active_cal": 0,
        "sleep_minutes": 0, "fat_percent": [], "lean_mass": [],
        "sleep_score": None, "bed_time": None, "wake_time": None,
        "height": None,
    })
    workouts_list = []
    height_value = None  # Latest height value

    # Track sleep sessions for time of bed/wake
    sleep_sessions = []

    try:
        for event, elem in ET.iterparse(io.BytesIO(xml_data), events=('end',)):
            if elem.tag == 'Record':
                rec_type = elem.get('type', '')
                start = elem.get('startDate', '')
                end = elem.get('endDate', '')
                value = elem.get('value', '0')
                source = elem.get('sourceName', '')

                try:
                    if not start:
                        elem.clear()
                        continue
                    d = start[:10]

                    if rec_type == 'HKQuantityTypeIdentifierBodyMass':
                        by_date[d]['weight'].append(float(value))
                    elif rec_type == 'HKQuantityTypeIdentifierHeight':
                        height_value = float(value)
                    elif rec_type == 'HKQuantityTypeIdentifierStepCount':
                        by_date[d]['steps'] += int(float(value))
                    elif rec_type == 'HKQuantityTypeIdentifierActiveEnergyBurned':
                        by_date[d]['active_cal'] += float(value)
                    elif rec_type == 'HKQuantityTypeIdentifierBodyFatPercentage':
                        by_date[d]['fat_percent'].append(float(value) * 100)
                    elif rec_type == 'HKQuantityTypeIdentifierLeanBodyMass':
                        by_date[d]['lean_mass'].append(float(value))
                    elif rec_type == 'HKCategoryTypeIdentifierSleepAnalysis':
                        val = elem.get('value', '')
                        if 'Asleep' in val or 'InBed' in val:
                            sleep_sessions.append({
                                'date': d,
                                'start': start,
                                'end': end,
                                'value': val,
                                'source': source,
                            })
                    # Apple Watch sleep quality (5-level: VeryHigh/High/Medium/Low/VeryLow)
                    elif rec_type == 'HKCategoryTypeIdentifierSleepAnalysis' or rec_type == 'HKQuantityTypeIdentifierSleepDurationGoal':
                        pass  # already handled above for sleep_minutes
                    elif 'SleepQuality' in rec_type or 'sleepQuality' in rec_type.lower():
                        # Apple sleep quality score
                        val = elem.get('value', '')
                        quality_map = {
                            'VeryHigh': 'очень высокое', 'High': 'высокое',
                            'Medium': 'среднее', 'Low': 'низкое', 'VeryLow': 'очень низкое'
                        }
                        for k, v in quality_map.items():
                            if k in val:
                                by_date[d]['sleep_score'] = v
                                break
                    elif 'SleepScore' in rec_type or 'sleepScore' in rec_type.lower():
                        try:
                            score = float(value)
                            if 0 <= score <= 100:
                                by_date[d]['sleep_score'] = score
                        except Exception:
                            pass
                except Exception:
                    pass
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
                except Exception:
                    pass
                elem.clear()
    except Exception as e:
        logger.error(f"XML parse error: {e}")
        return {"error": f"Ошибка парсинга: {e}"}

    # Process sleep sessions - find bed time and wake time
    sleep_by_date = defaultdict(lambda: {'minutes': 0, 'bed_time': None, 'wake_time': None})
    for s in sleep_sessions:
        d = s['date']
        try:
            start_dt = _parse_dt(s['start'])
            end_dt = _parse_dt(s['end'])
            if not start_dt or not end_dt:
                continue
            mins = (end_dt - start_dt).total_seconds() / 60
            sleep_by_date[d]['minutes'] += mins
            # Earliest bed time, latest wake time per day
            if sleep_by_date[d]['bed_time'] is None or start_dt < sleep_by_date[d]['bed_time']:
                sleep_by_date[d]['bed_time'] = start_dt
            if sleep_by_date[d]['wake_time'] is None or end_dt > sleep_by_date[d]['wake_time']:
                sleep_by_date[d]['wake_time'] = end_dt
        except Exception:
            pass

    for d, info in sleep_by_date.items():
        by_date[d]['sleep_minutes'] = info['minutes']
        if info['bed_time']:
            by_date[d]['bed_time'] = info['bed_time'].strftime('%H:%M')
        if info['wake_time']:
            by_date[d]['wake_time'] = info['wake_time'].strftime('%H:%M')

    # Detect anomalies
    anomalies = []
    clean_data = {}

    for d, data in by_date.items():
        clean = {
            'date': d,
            'weight': None,
            'fat_percent': None,
            'muscle_mass': None,
            'steps': data['steps'] if data['steps'] > 0 else None,
            'active_cal': round(data['active_cal']) if data['active_cal'] > 0 else None,
            'sleep_hours': None,
            'sleep_score': data['sleep_score'],
            'bed_time': data['bed_time'],
            'wake_time': data['wake_time'],
        }

        # Weight - median, check anomalies
        if data['weight']:
            sorted_w = sorted(data['weight'])
            median_w = sorted_w[len(sorted_w) // 2]
            if W_MIN <= median_w <= W_MAX:
                clean['weight'] = round(median_w, 1)
            else:
                anomalies.append({'date': d, 'type': 'weight', 'value': round(median_w, 1), 'reason': f'Вес {median_w}кг вне диапазона {W_MIN}-{W_MAX}'})

        # Steps anomaly
        if data['steps'] > STEPS_MAX:
            anomalies.append({'date': d, 'type': 'steps', 'value': data['steps'], 'reason': f'Шаги {data["steps"]:,} больше {STEPS_MAX:,}'})
            clean['steps'] = None

        # Sleep
        if data.get('sleep_minutes', 0) > 0:
            hours = round(data['sleep_minutes'] / 60, 1)
            if SLEEP_MIN_H <= hours <= SLEEP_MAX_H:
                clean['sleep_hours'] = hours
            else:
                anomalies.append({'date': d, 'type': 'sleep', 'value': hours, 'reason': f'Сон {hours}ч вне диапазона {SLEEP_MIN_H}-{SLEEP_MAX_H}'})

        # Fat %
        if data['fat_percent']:
            avg = sum(data['fat_percent']) / len(data['fat_percent'])
            if FAT_MIN <= avg <= FAT_MAX:
                clean['fat_percent'] = round(avg, 1)
            else:
                anomalies.append({'date': d, 'type': 'fat_percent', 'value': round(avg, 1), 'reason': f'% жира {avg}% вне диапазона {FAT_MIN}-{FAT_MAX}'})

        if data['lean_mass']:
            clean['muscle_mass'] = round(sum(data['lean_mass']) / len(data['lean_mass']), 1)

        clean_data[d] = clean

    return {
        'clean_data': clean_data,
        'anomalies': anomalies,
        'workouts': workouts_list,
        'height': height_value,
        'date_range': _get_date_range(by_date.keys()) if by_date else None,
        'total_days': len(by_date),
    }


def save_apple_health_data(user_id: int, parsed: dict, db: Database, remove_anomalies: bool = True) -> dict:
    """Save parsed data to DB. anomalies are skipped if remove_anomalies=True"""
    saved = {'weight': 0, 'activity': 0, 'sleep': 0, 'workouts': 0, 'body_comp': 0}

    clean_data = parsed['clean_data']

    for d, data in clean_data.items():
        if data.get('weight') is not None:
            try:
                db.log_weight_for_date(user_id, data['weight'], d)
                saved['weight'] += 1
            except Exception:
                pass

        if data.get('steps') is not None or data.get('active_cal') is not None:
            try:
                db.log_activity_for_date(user_id, {
                    'steps': data.get('steps') or 0,
                    'calories_burned': data.get('active_cal') or 0,
                    'source': 'apple_health_import',
                }, d)
                saved['activity'] += 1
            except Exception:
                pass

        if data.get('sleep_hours') is not None:
            try:
                quality_parts = []
                if data.get('sleep_score'):
                    quality_parts.append(f"score:{int(data['sleep_score'])}")
                if data.get('bed_time'):
                    quality_parts.append(f"bed:{data['bed_time']}")
                if data.get('wake_time'):
                    quality_parts.append(f"wake:{data['wake_time']}")
                quality = ', '.join(quality_parts) if quality_parts else None
                db.log_sleep(user_id, data['sleep_hours'], quality, d)
                saved['sleep'] += 1
            except Exception:
                pass

        if data.get('fat_percent') is not None:
            try:
                _save_body_composition(db, user_id, {
                    'date': d,
                    'weight': data.get('weight'),
                    'fat_percent': data['fat_percent'],
                    'muscle_mass': data.get('muscle_mass'),
                })
                saved['body_comp'] += 1
            except Exception:
                pass

    for w in parsed.get('workouts', []):
        try:
            db.log_workout_for_date(user_id, w, w['date'])
            saved['workouts'] += 1
        except Exception:
            pass

    # Save height to profile if found
    if parsed.get('height'):
        try:
            profile = db.get_user_profile(user_id) or {}
            profile['height'] = round(parsed['height'])
            db.save_user_profile(user_id, profile)
        except Exception:
            pass

    return saved


def parse_fatsecret_csv(csv_bytes: bytes, months_limit: int = 12) -> dict:
    """Parse FatSecret CSV - return data + days that should be filtered out"""
    text = None
    for enc in ['utf-8', 'utf-8-sig', 'cp1251', 'windows-1251']:
        try:
            text = csv_bytes.decode(enc)
            break
        except Exception:
            continue
    if not text:
        return {"error": "Не смог декодировать CSV"}

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"error": "CSV пустой"}

    first = rows[0]
    date_col = None
    for col in first.keys():
        if col and ('date' in col.lower() or 'дата' in col.lower()):
            date_col = col
            break
    if not date_col:
        date_col = list(first.keys())[0]

    cutoff = (date.today() - timedelta(days=30 * months_limit)).isoformat()

    by_date = defaultdict(list)
    for row in rows:
        try:
            d_str = row.get(date_col, '').strip()
            if not d_str:
                continue
            d = _parse_fatsecret_date(d_str)
            if not d or d < cutoff:
                continue
            by_date[d].append(row)
        except Exception:
            continue

    valid_days = {}
    skipped_days = []  # Each: {date, total_cal, reason}

    for d, day_rows in by_date.items():
        total_cal = 0
        for r in day_rows:
            total_cal += _safe_float(r, ['Calories', 'calories', 'Калории', 'ккал'])

        if MIN_CALORIES_PER_DAY <= total_cal <= MAX_CALORIES_PER_DAY:
            valid_days[d] = day_rows
        else:
            skipped_days.append({
                'date': d,
                'total_cal': round(total_cal),
                'reason': f'{round(total_cal)} ккал вне 1700-2700'
            })

    return {
        'valid_days': valid_days,
        'skipped_days': skipped_days,
        'total_rows': len(rows),
        'months_limit': months_limit,
    }


def save_fatsecret_data(user_id: int, parsed: dict, db: Database) -> dict:
    """Save valid days to DB"""
    saved_meals = 0
    saved_products = 0
    seen_products = set()

    for d, day_rows in parsed['valid_days'].items():
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

            desc = food + (f" ({portion})" if portion else "")
            try:
                db.log_meal_for_date(user_id, {
                    'description': desc,
                    'calories': round(cal),
                    'protein': round(prot, 1),
                    'fat': round(fat, 1),
                    'carbs': round(carbs, 1),
                    'time': _get_meal_time(meal_type),
                }, d)
                saved_meals += 1
            except Exception:
                pass

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
        'saved_meals': saved_meals,
        'saved_products': saved_products,
    }


# ============= helpers =============

def _save_body_composition(db: Database, user_id: int, data: dict):
    import json
    with db._conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS body_composition_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, date TEXT,
                weight REAL, fat_percent REAL, muscle_mass REAL,
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
            data.get('muscle_mass'), 'apple_health',
            json.dumps(data, ensure_ascii=False)
        ))


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
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except Exception:
            continue
    return None


def _parse_dt(s: str):
    """Parse ISO datetime string from Apple Health"""
    s = s.strip()
    # Format: "2024-04-23 07:00:00 +0300"
    try:
        # Replace " +" with "+" and remove timezone for simplicity
        s = re.sub(r'\s+([+-]\d{4})$', '', s)
        return datetime.fromisoformat(s.replace(' ', 'T'))
    except Exception:
        return None


def _get_meal_time(meal_type: str) -> str:
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


def _get_date_range(dates) -> dict:
    if not dates:
        return None
    sorted_d = sorted(dates)
    return {"from": sorted_d[0], "to": sorted_d[-1]}
