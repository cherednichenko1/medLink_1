"""CSRF, input validation and access rules shared by routes."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import abort, request, session

LOCAL_TZ = ZoneInfo('Europe/Kyiv')
EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def now_local():
    return datetime.now(LOCAL_TZ)


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


def protect_csrf():
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        expected = session.get('_csrf')
        supplied = request.form.get('csrf_token', '')
        if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
            abort(400, description='Форма застаріла або недійсна. Оновіть сторінку та спробуйте ще раз.')


def positive_id(value):
    try:
        number = int(value)
        if number <= 0:
            raise ValueError
        return number
    except (ValueError, TypeError):
        abort(400, description='Некоректний ідентифікатор.')


def valid_email(value):
    return bool(value and len(value) <= 254 and EMAIL_RE.fullmatch(value))


def require_role(role):
    if session.get('user_role') != role or not session.get('user_id'):
        abort(403, description='Увійдіть з відповідною роллю для цієї дії.')


def can_access_patient(cursor, patient_id):
    if session.get('user_role') == 'user':
        return session.get('user_id') == patient_id
    if session.get('user_role') == 'doctor':
        cursor.execute('''SELECT EXISTS (
            SELECT 1 FROM appointments WHERE user_id = %s AND doctor_id = %s AND status IN ('scheduled', 'in_progress', 'completed')
            UNION ALL SELECT 1 FROM patient_history WHERE patient_id = %s AND doctor_id = %s
        )''', (patient_id, session.get('user_id'), patient_id, session.get('user_id')))
        return cursor.fetchone()[0]
    return False


def require_patient_access(cursor, patient_id):
    if not can_access_patient(cursor, patient_id):
        abort(403, description='Немає доступу до даних цього пацієнта.')


def parse_slot(date_value, time_value, use_default_schedule=True):
    try:
        # Require exactly the format rendered by HTML inputs (no seconds/aliases).
        slot = datetime.strptime(f'{date_value} {time_value}', '%Y-%m-%d %H:%M').replace(tzinfo=LOCAL_TZ)
        if slot.strftime('%Y-%m-%d') != date_value or slot.strftime('%H:%M') != time_value:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Оберіть коректні дату та час.') from None
    now = now_local()
    if slot <= now or slot.date() > (now + timedelta(days=90)).date():
        raise ValueError('Оберіть майбутній час у межах наступних 90 днів.')
    if slot.minute not in (0, 30) or (use_default_schedule and (slot.weekday() >= 5 or not 9 <= slot.hour < 17)):
        raise ValueError('Прийом доступний у будні з 09:00 до 17:00, з інтервалом 30 хвилин.')
    return slot.date(), slot.time().replace(tzinfo=None)


def login_keys(role, login):
    # Trust the immediate nginx only when explicitly configured; never blindly
    # consume X-Forwarded-For sent by clients.
    address = request.remote_addr or 'unknown'
    return [hashlib.sha256(value.encode()).hexdigest()
            for value in (f'account:{role}:{login}', f'ip:{address}')]


def reserve_login_attempt(cursor, keys):
    """Row locks serialize attempts across replicas. Count before checking password."""
    cursor.execute("DELETE FROM login_attempts WHERE window_start < CURRENT_TIMESTAMP - INTERVAL '1 day'")
    allowed = True
    for key in sorted(keys):
        cursor.execute('''INSERT INTO login_attempts (key) VALUES (%s)
            ON CONFLICT (key) DO NOTHING''', (key,))
        cursor.execute('SELECT failures, window_start FROM login_attempts WHERE key = %s FOR UPDATE', (key,))
        failures, start = cursor.fetchone()
        if start < now_local() - timedelta(minutes=15):
            cursor.execute('UPDATE login_attempts SET failures = 0, window_start = CURRENT_TIMESTAMP WHERE key = %s', (key,))
            failures = 0
        if failures >= 10:
            allowed = False
        else:
            cursor.execute('UPDATE login_attempts SET failures = failures + 1 WHERE key = %s', (key,))
    return allowed
