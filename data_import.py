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
            # Try standard names first
            for name in zf.namelist():
                lower = name.lower()
                if lower.endswith("export.xml") and "apple_health" in lower:
                    xml_name = name
                    break
            # Try Russian "экспорт.xml" or any xml in apple_health_export folder
            if not xml_name:
                for name in zf.namelist():
                    lower = name.lower()
                    if "apple_health_export" in lower and name.endswith(".xml") and "cda" not in lower:
                        xml_name = name
                        break
            # Last resort - any non-CDA xml
            if not xml_name:
                for name in zf.namelist():
                    if name.endswith(".xml") and "cda" not in name.lower() and "ecg" not in name.lower():
                        xml_name = name
                        break
            if not xml_name:
                # Show what we found
                files = "\n".join(zf.namelist()[:20])
                return {"error": f"Не нашёл XML в архиве. Файлы:\n{files}"}
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
    """Parse FatSecret 'Food Diary Report - Detailed Report' CSV.

    Format example (Russian export):
    #--- Period Summary ---
    Дата,Кал ( ккал),Жир( г),...
    "понедельник, мая 4, 2026",2021,...     <- DAY TOTAL row
     Завтрак,555,...                          <- MEAL TYPE row (starts with space)
      ВкусВилл Тартин,43,...                  <- FOOD row (starts with 2 spaces)
       21 г                                   <- PORTION row
    """
    text = None
    for enc in ['utf-8-sig', 'utf-8', 'cp1251', 'windows-1251']:
        try:
            text = csv_bytes.decode(enc)
            break
        except Exception:
            continue
    if not text:
        return {"error": "Не смог декодировать CSV"}

    lines = text.split("\n")
    cutoff = (date.today() - timedelta(days=30 * months_limit)).isoformat()

    # Find start of Report Details section
    details_start = -1
    for i, line in enumerate(lines):
        if "Report Details" in line or "Дата,Кал" in line.replace(" ", ""):
            details_start = i
            break
    if details_start < 0:
        return {"error": "Не нашёл секцию Report Details в CSV"}

    # Parse from there
    by_date = {}  # date -> {total_cal, meals: [{description, calories, protein, fat, carbs, meal_type}]}
    current_date = None
    current_meal_type = None
    current_food = None
    valid_days_count = 0

    # Use csv reader for proper parsing of quoted fields
    reader = csv.reader(io.StringIO("\n".join(lines[details_start:])))

    for row in reader:
        if not row or not row[0].strip():
            continue

        first = row[0]
        # Detect what kind of row this is by leading spaces in original line
        # Day total: starts non-space, like "понедельник, мая 4, 2026"
        # Meal type: starts with 1 space, like " Завтрак"
        # Food: starts with 2 spaces, like "  ВкусВилл..."
        # Portion: starts with 3 spaces, like "   21 г"

        leading_spaces = len(first) - len(first.lstrip(" "))
        text_value = first.strip()

        # Skip header
        if "Дата" in text_value or "Кал" in text_value and "ккал" in (text_value if not text_value.replace(" ", "").startswith("Кал(") else ""):
            continue
        if text_value.startswith("#"):
            continue

        # Day row - has date format like "понедельник, мая 4, 2026"
        if leading_spaces == 0 and "," in text_value:
            d = _parse_russian_date(text_value)
            if d:
                current_date = d
                try:
                    total_cal = float(row[1].replace(",", ".")) if len(row) > 1 else 0
                except Exception:
                    total_cal = 0
                if d >= cutoff:
                    by_date[d] = {
                        'total_cal': total_cal,
                        'meals': []
                    }
                else:
                    current_date = None
                continue

        if not current_date or current_date not in by_date:
            continue

        # Meal type row (1 leading space)
        if leading_spaces == 1 and text_value in ("Завтрак", "Обед", "Ужин", "Перекус/Другое", "Перекус"):
            current_meal_type = text_value
            current_food = None
            continue

        # Food row (2 leading spaces) - has description and nutrition
        if leading_spaces == 2 and len(row) >= 5:
            try:
                name = text_value
                cal = _safe_float_str(row[1] if len(row) > 1 else "")
                fat = _safe_float_str(row[2] if len(row) > 2 else "")
                carbs = _safe_float_str(row[4] if len(row) > 4 else "")
                prot = _safe_float_str(row[7] if len(row) > 7 else "")

                current_food = {
                    'name': name,
                    'calories': cal,
                    'fat': fat,
                    'carbs': carbs,
                    'protein': prot,
                    'meal_type': current_meal_type or "Прочее",
                    'portion_text': "",
                    'portion_g': None,
                }
                continue
            except Exception:
                continue

        # Portion row (3+ spaces) - attach to last food
        if leading_spaces >= 3 and current_food:
            current_food['portion_text'] = text_value
            current_food['portion_g'] = _extract_grams(text_value)

            if current_food['calories'] > 0:
                by_date[current_date]['meals'].append({
                    'product_name': current_food['name'],
                    'portion_text': current_food['portion_text'],
                    'portion_g': current_food['portion_g'],
                    'calories': round(current_food['calories']),
                    'protein': round(current_food['protein'], 1),
                    'fat': round(current_food['fat'], 1),
                    'carbs': round(current_food['carbs'], 1),
                    'meal_type': current_food['meal_type'],
                })
            current_food = None

    # Filter days by daily calories
    valid_days = {}
    skipped_days = []

    for d, info in by_date.items():
        total = info.get('total_cal', 0)
        if MIN_CALORIES_PER_DAY <= total <= MAX_CALORIES_PER_DAY:
            valid_days[d] = info['meals']
        else:
            skipped_days.append({
                'date': d,
                'total_cal': round(total),
                'reason': f'{round(total)} ккал вне 1700-2700'
            })

    return {
        'valid_days': valid_days,  # Now: dict[date -> list of meal dicts]
        'skipped_days': skipped_days,
        'total_rows': len(lines),
        'months_limit': months_limit,
    }



def _extract_grams(text: str) -> float:
    """Extract grams from portion string. '21 г' -> 21.0. '182 мл' -> 182.0 (treat ml as g for liquids)"""
    if not text:
        return None
    # Try to match patterns: "21 г", "1.5 кг", "182 мл", "2 средний"
    m = re.search(r'(\d+[.,]?\d*)\s*(г|кг|мл|л|gram|kg|ml)', text.lower())
    if m:
        val = float(m.group(1).replace(',', '.'))
        unit = m.group(2)
        if unit in ('кг', 'kg', 'л'):
            return val * 1000
        return val
    # Just a number?
    m = re.search(r'^(\d+[.,]?\d*)', text.strip())
    if m:
        try:
            return float(m.group(1).replace(',', '.'))
        except Exception:
            pass
    return None

def _safe_float_str(s: str) -> float:
    """Parse number from Russian-style string with comma as decimal"""
    if not s:
        return 0.0
    try:
        cleaned = str(s).strip().replace(",", ".")
        cleaned = re.sub(r'[^\d.\-]', '', cleaned)
        return float(cleaned) if cleaned else 0.0
    except Exception:
        return 0.0


RUSSIAN_MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
    'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12,
    'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4, 'май': 5, 'июнь': 6,
    'июль': 7, 'август': 8, 'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12,
}


def _parse_russian_date(s: str) -> str:
    """Parse 'понедельник, мая 4, 2026' or 'мая 4, 2026' -> '2026-05-04'"""
    s = s.strip().lower()
    # Remove day name if present
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        # Filter out day names (start with letters but no digits)
        parts = [p for p in parts if not p in ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')]
        s = " ".join(parts)

    # Now should be like "мая 4 2026" or "4 мая 2026"
    tokens = re.findall(r'\w+', s)
    month = None
    day = None
    year = None
    for tok in tokens:
        if tok in RUSSIAN_MONTHS:
            month = RUSSIAN_MONTHS[tok]
        elif tok.isdigit():
            v = int(tok)
            if v > 1900:
                year = v
            elif 1 <= v <= 31:
                day = v

    if month and day and year:
        try:
            return date(year, month, day).isoformat()
        except Exception:
            return None
    return None


def save_fatsecret_data(user_id: int, parsed: dict, db: Database) -> dict:
    """
    Save parsed days to DB:
    1. Aggregate all entries per product → calculate per-100g KBJU + standard portion (median)
    2. Filter outliers (>5% deviation from median per-100g)
    3. Save products with median values
    4. Save individual meal_logs with actual portion_g and actual KBJU per portion
    """
    from collections import defaultdict
    from statistics import median

    # Group all entries by normalized product name
    product_entries = defaultdict(list)  # name_norm -> list of {portion_g, calories, protein, fat, carbs, original_name}

    for d, meals in parsed['valid_days'].items():
        for m in meals:
            name = m.get('product_name', '').strip()
            if not name:
                continue
            # Normalize: lowercase, remove gram suffixes from name itself
            name_norm = name.lower()
            name_norm = re.sub(r'\s*\d+\s*(г|гр|мл|kg|кг|шт|штук|порц|средний|маленький|большой)\s*$', '', name_norm).strip()

            product_entries[name_norm].append({
                'original_name': name,
                'portion_g': m.get('portion_g'),
                'calories': m['calories'],
                'protein': m['protein'],
                'fat': m['fat'],
                'carbs': m['carbs'],
                'date': d,
                'meal_type': m.get('meal_type', ''),
                'portion_text': m.get('portion_text', ''),
            })

    # Calculate per-100g and median values for each product
    products_to_save = {}  # name_norm -> {name, cal_100g, prot_100g, fat_100g, carbs_100g, std_portion_g}

    for name_norm, entries in product_entries.items():
        # Calculate per-100g for each entry that has portion_g
        per_100g_values = []
        portion_grams = []

        for e in entries:
            pg = e.get('portion_g')
            if not pg or pg <= 0:
                continue
            scale = 100.0 / pg
            per_100g_values.append({
                'cal': e['calories'] * scale,
                'prot': e['protein'] * scale,
                'fat': e['fat'] * scale,
                'carbs': e['carbs'] * scale,
            })
            portion_grams.append(pg)

        if not per_100g_values:
            continue

        # Median per-100g
        med_cal_100g = median([v['cal'] for v in per_100g_values])
        med_prot_100g = median([v['prot'] for v in per_100g_values])
        med_fat_100g = median([v['fat'] for v in per_100g_values])
        med_carbs_100g = median([v['carbs'] for v in per_100g_values])

        # Filter outliers for portion size (>2x or <0.5x of median)
        if portion_grams:
            med_portion = median(portion_grams)
            valid_portions = [p for p in portion_grams if 0.5 * med_portion <= p <= 2.0 * med_portion]
            std_portion = median(valid_portions) if valid_portions else med_portion
        else:
            std_portion = None

        # Use most common original name (longest version)
        original_names = sorted(set(e['original_name'] for e in entries), key=len, reverse=True)
        best_name = original_names[0]

        products_to_save[name_norm] = {
            'name': best_name,
            'name_norm': name_norm,
            'calories_per_100g': round(med_cal_100g),
            'protein_per_100g': round(med_prot_100g, 1),
            'fat_per_100g': round(med_fat_100g, 1),
            'carbs_per_100g': round(med_carbs_100g, 1),
            'standard_portion_g': round(std_portion, 1) if std_portion else None,
            'use_count': len(entries),
        }

    # Save products
    saved_products = 0
    for name_norm, p in products_to_save.items():
        try:
            db.upsert_product_with_per100g(user_id, p)
            saved_products += 1
        except Exception as e:
            logger.error(f"product save err: {e}")

    # Save meal logs
    saved_meals = 0
    for d, meals in parsed['valid_days'].items():
        for m in meals:
            try:
                name = m.get('product_name', '').strip()
                name_norm = name.lower()
                name_norm = re.sub(r'\s*\d+\s*(г|гр|мл|kg|кг|шт|штук|порц|средний|маленький|большой)\s*$', '', name_norm).strip()

                # Build description: just product name (without grams duplicating in name)
                desc = name
                if m.get('portion_text'):
                    desc = f"{name} ({m['portion_text']})"

                db.log_meal_for_date(user_id, {
                    'description': desc,
                    'product_name': name,
                    'portion_g': m.get('portion_g'),
                    'calories': m['calories'],
                    'protein': m['protein'],
                    'fat': m['fat'],
                    'carbs': m['carbs'],
                    'time': _get_meal_time(m.get('meal_type', '')),
                }, d)
                saved_meals += 1
            except Exception as e:
                logger.error(f"meal save err: {e}")

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
