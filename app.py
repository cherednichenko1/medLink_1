import io
import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from urllib.parse import urlsplit

import psycopg2
import qrcode
import click
from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for, jsonify
from qrcode.image.svg import SvgPathImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from itsdangerous import URLSafeTimedSerializer, BadSignature
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from db import close_db, get_db, init_db
from security import (can_access_patient, csrf_token, login_keys, now_local, parse_slot,
                      positive_id, protect_csrf, require_patient_access, require_role,
                      reserve_login_attempt, valid_email)

app = Flask(__name__)
secret = os.environ.get('FLASK_SECRET_KEY', '')
if len(secret) < 32 or secret in {'local_dev_secret_change_me', 'your_super_secret_key_here', 'change_this_to_something_random'}:
    raise RuntimeError('Задайте FLASK_SECRET_KEY: випадковий секрет щонайменше 32 символи.')
app.config.update(
    SECRET_KEY=secret,
    SESSION_COOKIE_NAME='medlink_session_v2',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
    MAX_CONTENT_LENGTH=64 * 1024,
)
# Compose/minikube: nginx is the one immediate trusted hop. Do not enable for
# deployments where clients can reach Gunicorn directly.
if os.environ.get('TRUST_PROXY', 'false').lower() == 'true':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
from doctor_workflows import bp as doctor_blueprint
app.register_blueprint(doctor_blueprint)
from portal import bp as portal_blueprint
app.register_blueprint(portal_blueprint)
from communications import bp as communications_blueprint
app.register_blueprint(communications_blueprint)
@app.before_request
def upload_request_limit():
    if request.endpoint == 'portal.upload_avatar' and request.method == 'POST':
        request.max_content_length = 3 * 1024 * 1024
    if request.endpoint == 'portal.documents' and request.method == 'POST':
        request.max_content_length = 6 * 1024 * 1024
app.teardown_appcontext(close_db)
app.before_request(protect_csrf)
app.jinja_env.globals['csrf_token'] = csrf_token
app.jinja_env.filters['strftime'] = lambda dt, fmt: now_local().strftime(fmt) if dt == 'now' else dt.strftime(fmt)
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'http://localhost:8080').rstrip('/')
parsed_public_url = urlsplit(PUBLIC_BASE_URL)
if parsed_public_url.scheme not in {'http', 'https'} or not parsed_public_url.netloc or parsed_public_url.query or parsed_public_url.fragment or parsed_public_url.username:
    raise RuntimeError('PUBLIC_BASE_URL має бути адресою http(s) без облікових даних, query або fragment.')
app.jinja_env.globals['qr_base_host'] = parsed_public_url.hostname
PASSWORD_METHOD = 'pbkdf2:sha256:1000000'
# Equivalent work for unknown accounts to reduce account enumeration by timing.
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32), method=PASSWORD_METHOD)


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer' if request.endpoint == 'shared_page' else 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.endpoint != 'static':
        response.headers['Cache-Control'] = 'no-store'
    if app.config['SESSION_COOKIE_SECURE']:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.errorhandler(psycopg2.Error)
def database_error(error):
    # Do not expose SQL, credentials or personal data in responses or logs.
    app.logger.error('Database operation failed (%s)', type(error).__name__)
    return render_template('error.html', message='Не вдалося виконати операцію. Спробуйте пізніше.', code=503), 503


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(409)
@app.errorhandler(413)
@app.errorhandler(429)
def http_error(error):
    messages = {404: 'Сторінку не знайдено.', 405: 'Ця дія потребує іншого методу запиту.',
                413: 'Надіслано забагато даних.', 429: 'Забагато спроб. Спробуйте через 15 хвилин.'}
    return render_template('error.html', code=error.code,
                           message=messages.get(error.code, error.description)), error.code


@app.errorhandler(500)
def server_error(error):
    return render_template('error.html', code=500, message='Внутрішня помилка. Спробуйте пізніше.'), 500


@app.route('/')
def main():
    return render_template('home.html')


@app.route('/helsiPage')
def helsiPage():
    return render_template('medlink.html')


@app.route('/loginPage')
def loginPage():
    return render_template('login.html', next_url=safe_next(request.args.get('next', '')), doctor_only=False)


@app.route('/doctor/login')
def doctor_login():
    return render_template('login.html', next_url=safe_next(request.args.get('next', '')), doctor_only=True)


def registration_form(error=None, status=200):
    with get_db().cursor() as cursor:
        cursor.execute('SELECT id, name FROM hospital ORDER BY name')
        hospitals = cursor.fetchall()
        cursor.execute('SELECT id, name FROM specialization ORDER BY name')
        specializations = cursor.fetchall()
    return render_template('registerForm.html', hospitals=hospitals, specializations=specializations,
                           error=error, values=request.form if request.method == "POST" else request.args, doctor_registration_enabled=True), status


@app.route('/registerPage', methods=['GET', 'POST'])
def registerPage():
    if request.method == 'GET':
        return registration_form()
    role = request.form.get('role')
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    phone = request.form.get('phone', '').strip()
    if role not in {'user', 'doctor'}:
        return registration_form('Оберіть роль.', 400)
    if not 2 <= len(name) <= 150 or not valid_email(email) or not 5 <= len(phone) <= 30:
        return registration_form('Перевірте ім’я, email і телефон.', 400)
    if not 12 <= len(password) <= 128 or password != request.form.get('confirm_password'):
        return registration_form('Пароль має містити 12–128 символів; підтвердження має збігатися.', 400)
    params = [name, email, phone, None]
    if role == 'doctor':
        rnokpp = request.form.get('rnokpp', '').strip()
        if len(rnokpp) != 10 or not rnokpp.isascii() or not rnokpp.isdigit():
            return registration_form('РНОКПП має містити 10 цифр.', 400)
        try:
            experience = int(request.form.get('experience', ''))
            hospital_id = int(request.form.get('hospital', ''))
            specialization_id = int(request.form.get('specialization', ''))
            if not 0 <= experience <= 80 or min(hospital_id, specialization_id) <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return registration_form('Перевірте досвід, лікарню та спеціалізацію.', 400)
        params = [name, email, rnokpp, phone, experience, hospital_id, specialization_id, params[-1]]
    conn = get_db()
    with conn.cursor() as cursor:
        allowed = reserve_login_attempt(cursor, login_keys('registration', email))
    conn.commit()
    if not allowed:
        abort(429)
    params[-1] = generate_password_hash(password, method=PASSWORD_METHOD)
    try:
        with conn.cursor() as cursor:
            if role == 'user':
                cursor.execute('''INSERT INTO "user" (name, email, phone, password)
                    VALUES (%s, %s, %s, %s)''', params)
            else:
                cursor.execute('''INSERT INTO doctor (full_name, email, rnokpp, phone, experience,
                    hospital_id, specialization_id, password) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''', params)
                new_doctor_id = cursor.fetchone()[0]
                cursor.execute('''INSERT INTO doctor_working_hours(doctor_id,weekday,starts_at,ends_at)
                    SELECT %s, day, '09:00'::time, '17:00'::time FROM generate_series(0,4) day''', (new_doctor_id,))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        return registration_form('Не вдалося зареєструватися. Перевірте дані або зверніться до адміністратора.', 400)
    flash('Обліковий запис створено. Тепер увійдіть.', 'success')
    return redirect(url_for('doctor_login' if role == 'doctor' else 'loginPage'), code=303)


@app.route('/getLogin', methods=['POST'])
def get_login():
    destination = safe_next(request.form.get('next', ''))
    role = request.form.get('role')
    login = request.form.get('enterLogin', '').strip().lower()
    password = request.form.get('enterPassword', '')
    if role not in {'user', 'doctor'} or not login or len(login) > 254 or not 1 <= len(password) <= 128:
        flash('Перевірте роль, логін і пароль.', 'error')
        return redirect(url_for('doctor_login' if role == 'doctor' else 'loginPage', next=destination), code=303)
    conn = get_db()
    keys = login_keys(role, login)
    with conn.cursor() as cursor:
        allowed = reserve_login_attempt(cursor, keys)
    conn.commit()  # reserve attempts before slow hashing; shared by all pods
    if not allowed:
        abort(429)
    with conn.cursor() as cursor:
        if role == 'doctor':
            cursor.execute('''SELECT id, full_name, password FROM doctor
                WHERE lower(email) = %s OR rnokpp = %s ORDER BY id LIMIT 1''', (login, login))
        else:
            cursor.execute('SELECT id, name, password FROM "user" WHERE lower(email) = %s', (login,))
        row = cursor.fetchone()
    valid = check_password_hash((row[2] if row and row[2] else DUMMY_HASH), password)
    if not row or not valid:
        flash('Невірний логін або пароль.', 'error')
        return redirect(url_for('doctor_login' if role == 'doctor' else 'loginPage', next=destination), code=303)
    session.clear()
    session.permanent = True
    session.update(user_role=role, user_id=row[0])
    csrf_token()
    flash('Вхід виконано.', 'success')
    return redirect(destination or url_for('doctor_cabinet' if role == 'doctor' else 'user_cabinet'), code=303)


@app.route('/searchPage', methods=['GET', 'POST'])
def searchPage():
    if request.method == 'POST':
        return book_appointment()
    try:
        page = int(request.args.get('page', '1'))
        if not 1 <= page <= 10000:
            raise ValueError
    except ValueError:
        abort(400, description='Некоректний номер сторінки.')
    with get_db().cursor() as cursor:
        cursor.execute('SELECT DISTINCT type FROM hospital ORDER BY type')
        hospital_types = [row[0] for row in cursor.fetchall()]
        cursor.execute('SELECT name FROM district ORDER BY name')
        districts = [row[0] for row in cursor.fetchall()]
        cursor.execute('SELECT name FROM specialization ORDER BY name')
        specializations = [row[0] for row in cursor.fetchall()]
        query = '''SELECT d.id, d.full_name, d.phone, d.experience, h.name, h.type,
            district.name, h.address, h.edrpou, s.name, d.email FROM doctor d
            JOIN hospital h ON d.hospital_id = h.id JOIN district ON h.district_id = district.id
            JOIN specialization s ON d.specialization_id = s.id WHERE TRUE'''
        params = []
        experience = request.args.get('experience', '')
        if experience:
            try:
                experience = int(experience)
                if not 0 <= experience <= 80:
                    raise ValueError
            except ValueError:
                abort(400, description='Досвід має бути числом від 0 до 80.')
            query += ' AND d.experience >= %s'
            params.append(experience)
        for name, column in [('hospital_type', 'h.type'), ('district', 'district.name'), ('specialization', 's.name')]:
            value = request.args.get(name, '').strip()
            if value:
                query += f' AND {column} = %s'
                params.append(value)
        query += ' ORDER BY d.full_name, d.id LIMIT 51 OFFSET %s'
        params.append((page - 1) * 50)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        has_next = len(rows) > 50
        doctors = rows[:50]
        # One query for all doctors rather than one extra query per doctor.
        cursor.execute('''SELECT doctor_id, appointment_date, appointment_time FROM appointments
            WHERE doctor_id = ANY(%s) AND status IN ('scheduled','in_progress') AND appointment_date >= %s
            ORDER BY appointment_date, appointment_time''', ([d[0] for d in doctors], now_local().date()))
        doctor_appointments = {}
        for doctor_id, date, time in cursor.fetchall():
            doctor_appointments.setdefault(doctor_id, []).append((date, time))
        cursor.execute('SELECT doctor_id,weekday,starts_at,ends_at FROM doctor_working_hours WHERE doctor_id = ANY(%s) ORDER BY weekday', ([d[0] for d in doctors],))
        working_hours = {}
        for id,day,start,end in cursor.fetchall():
            working_hours.setdefault(id,[]).append((day,start,end))
    today = now_local().date()
    return render_template('searchForm.html', doctors=doctors, hospital_types=hospital_types,
        districts=districts, specializations=specializations, doctor_appointments=doctor_appointments,
        working_hours=working_hours, page=page, has_next=has_next, today=today.isoformat(), max_date=(today + timedelta(days=90)).isoformat(), filters=request.args, next_url=url_for('searchPage', **dict(request.args.to_dict(), page=page + 1)),
        previous_url=url_for('searchPage', **dict(request.args.to_dict(), page=max(1, page - 1))))


def book_appointment():
    require_role('user')
    doctor_id = positive_id(request.form.get('doctor_id'))
    try:
        date, time = parse_slot(request.form.get('appointment_date'), request.form.get('appointment_time'), use_default_schedule=False)
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('searchPage'), code=303)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT id FROM doctor WHERE id = %s FOR UPDATE', (doctor_id,))
            if not cursor.fetchone():
                abort(404)
            cursor.execute('''SELECT starts_at, ends_at FROM doctor_working_hours
                WHERE doctor_id = %s AND weekday = %s''', (doctor_id, date.weekday()))
            hours = cursor.fetchone()
            slot_end = datetime.combine(date, time) + timedelta(minutes=30)
            if not hours or time < hours[0] or slot_end.date() != date or slot_end.time() > hours[1]:
                conn.rollback()
                flash('Лікар не працює в цей час. Перевірте його графік.', 'error')
                return redirect(url_for('searchPage'), code=303)
            cursor.execute('''INSERT INTO appointments (doctor_id, user_id, appointment_date, appointment_time)
                VALUES (%s,%s,%s,%s)''', (doctor_id, session['user_id'], date, time))
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash('Цей час уже зайнятий. Оберіть інший.', 'error')
        return redirect(url_for('searchPage'), code=303)
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        abort(404)
    flash('Вас записано на прийом. Деталі доступні в особистому кабінеті.', 'success')
    return redirect(url_for('user_cabinet'), code=303)


@app.route('/appointments/<int:appointment_id>/cancel', methods=['POST'])
def cancel_appointment(appointment_id):
    require_role('user')
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute('''UPDATE appointments SET status = 'cancelled'
            WHERE id = %s AND user_id = %s AND status = 'scheduled'
            AND (appointment_date + appointment_time) > %s RETURNING id''',
            (appointment_id, session['user_id'], now_local().replace(tzinfo=None)))
        if not cursor.fetchone():
            abort(404)
    conn.commit()
    flash('Запис на прийом скасовано.', 'success')
    return redirect(url_for('user_cabinet'), code=303)


def doctor_record(cursor, doctor_id):
    cursor.execute('''SELECT d.full_name, d.phone, d.experience, d.email, s.name, h.name
        FROM doctor d JOIN specialization s ON d.specialization_id = s.id
        JOIN hospital h ON d.hospital_id = h.id WHERE d.id = %s''', (doctor_id,))
    row = cursor.fetchone()
    if not row:
        abort(404)
    return dict(zip(('full_name', 'phone', 'experience', 'email', 'specialization', 'hospital'), row))


@app.route('/doctorCabinet', methods=['GET', 'POST'])
def doctor_cabinet():
    if session.get('user_role') != 'doctor':
        return redirect(url_for('doctor_login'))
    patient_value = request.form.get('patient_id') if request.method == 'POST' else request.args.get('patient_id')
    patient_id = positive_id(patient_value) if patient_value else None
    conn = get_db()
    with conn.cursor() as cursor:
        doctor = doctor_record(cursor, session['user_id'])
        cursor.execute('''SELECT u.id, u.name FROM "user" u WHERE EXISTS (
            SELECT 1 FROM appointments a WHERE a.user_id = u.id AND a.doctor_id = %s AND a.status IN ('scheduled','in_progress','completed')
        ) OR EXISTS (SELECT 1 FROM patient_history ph WHERE ph.patient_id = u.id AND ph.doctor_id = %s)
        ORDER BY u.name''', (session['user_id'], session['user_id']))
        patients = cursor.fetchall()
        cursor.execute('''SELECT a.appointment_date, a.appointment_time, u.name FROM appointments a
            LEFT JOIN "user" u ON u.id = a.user_id WHERE a.doctor_id = %s AND a.status IN ('scheduled','in_progress')
            AND (a.appointment_date + a.appointment_time) > %s ORDER BY a.appointment_date, a.appointment_time''',
            (session['user_id'], now_local().replace(tzinfo=None)))
        appointments = cursor.fetchall()
        history = []
        if patient_id:
            require_patient_access(cursor, patient_id)
            cursor.execute('''SELECT patient_id, recommendations, medication, timestamp, diagnosis FROM patient_history
                WHERE doctor_id = %s AND patient_id = %s ORDER BY timestamp DESC''', (session['user_id'], patient_id))
            history = cursor.fetchall()
        if request.method == 'POST':
            if not patient_id:
                abort(400, description='Оберіть пацієнта.')
            recommendations = request.form.get('recommendations', '').strip()
            medication = request.form.get('medication', '').strip()
            if not (recommendations or medication) or max(len(recommendations), len(medication)) > 5000:
                flash('Заповніть рекомендації або ліки (до 5000 символів у полі).', 'error')
            else:
                cursor.execute('''INSERT INTO patient_history (patient_id, doctor_id, recommendations, medication)
                    VALUES (%s,%s,%s,%s)''', (patient_id, session['user_id'], recommendations, medication))
                conn.commit()
                flash('Запис до медичної історії додано.', 'success')
            return redirect(url_for('doctor_cabinet', patient_id=patient_id), code=303)
    return render_template('doctorCabinet.html', doctor=doctor, patients=patients, history=history,
                           selected_patient_id=patient_id, appointments=appointments)


@app.route('/userCabinet')
def user_cabinet():
    if session.get('user_role') != 'user':
        return redirect(url_for('loginPage'))
    with get_db().cursor() as cursor:
        cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (session['user_id'],))
        row = cursor.fetchone()
        if not row:
            session.clear()
            return redirect(url_for('loginPage'))
        user = dict(zip(('name', 'email', 'phone'), row))
        cursor.execute('''SELECT a.id, d.full_name, a.appointment_date, a.appointment_time, a.status
            FROM appointments a JOIN doctor d ON a.doctor_id = d.id WHERE a.user_id = %s
            ORDER BY a.appointment_date DESC, a.appointment_time DESC''', (session['user_id'],))
        appointments = cursor.fetchall()
    return render_template('userCabinet.html', user=user, user_name=user['name'], appointments=appointments,
                           current_time=now_local().replace(tzinfo=None))


@app.route('/doctor_info/<email>')
def doctor_info(email):
    with get_db().cursor() as cursor:
        cursor.execute('SELECT id FROM doctor WHERE lower(email) = %s', (email.lower(),))
        row = cursor.fetchone()
        if not row:
            abort(404)
        doctor = doctor_record(cursor, row[0])
    return render_template('doctor_info.html', doctor=doctor)


@app.route('/user_info/<int:user_id>')
def user_info(user_id):
    if session.get('user_role') not in {'user', 'doctor'}:
        return redirect(url_for('loginPage', next=request.path))
    with get_db().cursor() as cursor:
        require_patient_access(cursor, user_id)
        cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (user_id,))
        row = cursor.fetchone()
        if not row:
            abort(404)
    return render_template('user_info.html', user=dict(zip(('name', 'email', 'phone'), row)), patient_id=user_id)


def patient_records(patient_id):
    if session.get('user_role') not in {'user', 'doctor'}:
        abort(403, description='Для перегляду медичної історії потрібно увійти.')
    with get_db().cursor() as cursor:
        require_patient_access(cursor, patient_id)
        cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (patient_id,))
        row = cursor.fetchone()
        if not row:
            abort(404)
        query = 'SELECT recommendations, medication, timestamp, diagnosis FROM patient_history WHERE patient_id = %s'
        params = [patient_id]
        if session['user_role'] == 'doctor':
            query += ' AND doctor_id = %s'
            params.append(session['user_id'])
        cursor.execute(query + ' ORDER BY timestamp DESC', params)
        history = cursor.fetchall()
    return dict(zip(('name', 'email', 'phone'), row)), history


@app.route('/patient_history/<int:patient_id>')
def patient_history(patient_id):
    if session.get('user_role') not in {'user', 'doctor'}:
        return redirect(url_for('loginPage', next=request.path))
    patient, history = patient_records(patient_id)
    return render_template('patient_history.html', patient=patient, history=history, patient_id=patient_id)


def safe_next(value):
    # Only known private read pages; no open redirects or protocol-relative URLs.
    import re
    if value in {'/workspace','/profile','/messages'}:
        return value
    if re.fullmatch(r'/(?:messages/[1-9][0-9]*|calls/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|patients/[1-9][0-9]*/documents|user_info/[1-9][0-9]*|patient_history/[1-9][0-9]*|doctor/patients/[1-9][0-9]*/history)', value or ''):
        return value
    return ''


def share_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt='medlink-qr-share-v1', signer_kwargs={'digest_method': hashlib.sha256})


def decode_share(token):
    try:
        data = share_serializer().loads(token, max_age=900)
    except BadSignature:
        abort(403, description='Посилання недійсне або його 15-хвилинний термін минув. Попросіть новий QR-код.')
    if not isinstance(data, dict) or data.get('v') != 1 or data.get('scope') not in {'profile', 'history'}:
        abort(403)
    if type(data.get('patient_id')) is not int or data['patient_id'] <= 0:
        abort(403)
    if data.get('doctor_id') is not None and (type(data['doctor_id']) is not int or data['doctor_id'] <= 0):
        abort(403)
    return data


@app.route('/qr/share', methods=['POST'])
def create_qr_share():
    scope = request.form.get('scope')
    if scope not in {'profile', 'history'}:
        abort(400)
    patient_id = positive_id(request.form.get('patient_id'))
    if scope == 'profile':
        require_role('user')
        if patient_id != session['user_id']:
            abort(403)
        doctor_id = None
    else:
        patient_records(patient_id)  # authorize issuer, never accept doctor scope from the client
        doctor_id = session['user_id'] if session['user_role'] == 'doctor' else None
    token = share_serializer().dumps({'v': 1, 'scope': scope, 'patient_id': patient_id,
        'doctor_id': doctor_id, 'nonce': secrets.token_urlsafe(12)})
    target = public_url('shared_page', token=token)
    # Gateway/Gunicorn logging omits query strings; the template clears the token from history.
    qr = qrcode.make(target, image_factory=SvgPathImage, box_size=7, border=4)
    stream = io.BytesIO()
    qr.save(stream)
    return jsonify(image='data:image/svg+xml;base64,' + base64.b64encode(stream.getvalue()).decode(),
                   url=target, expires_in=900)


@app.route('/shared', methods=['GET', 'POST'])
def shared_page():
    token = request.args.get('token', '') if request.method == 'GET' else request.form.get('token', '')
    if request.method == 'GET' and not token:
        return render_template('shared.html')  # backward compatibility with old fragment QR
    data = decode_share(token)
    with get_db().cursor() as cursor:
        cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (data['patient_id'],))
        row = cursor.fetchone()
        if not row:
            abort(404)
        patient = dict(zip(('name', 'email', 'phone'), row))
        if data['scope'] == 'profile':
            return render_template('user_info.html', user=patient, shared=True, patient_id=data['patient_id'])
        query = 'SELECT recommendations, medication, timestamp, diagnosis FROM patient_history WHERE patient_id = %s'
        params = [data['patient_id']]
        if data['doctor_id'] is not None:
            query += ' AND doctor_id = %s'
            params.append(data['doctor_id'])
        cursor.execute(query + ' ORDER BY timestamp DESC', params)
        history = cursor.fetchall()
    return render_template('patient_history.html', patient=patient, history=history, shared=True, patient_id=data['patient_id'])


def public_url(endpoint, **values):
    return PUBLIC_BASE_URL + url_for(endpoint, **values)


def qr_response(target, filename):
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(target)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    stream = io.BytesIO()
    image.save(stream)
    stream.seek(0)
    return send_file(stream, mimetype='image/svg+xml', as_attachment=False, download_name=filename)


@app.route('/generate_qr_doctor/<email>')
def generate_qr_doctor(email):
    if not valid_email(email):
        abort(400, description='Некоректний email.')
    return qr_response(public_url('doctor_info', email=email), 'doctor_qr.svg')


@app.route('/generate_qr_user')
def generate_qr_user():
    require_role('user')
    return qr_response(public_url('user_info', user_id=session['user_id']), 'user_qr.svg')


@app.route('/generate_qr_patient_history/<int:patient_id>')
def generate_qr_patient_history(patient_id):
    patient_records(patient_id)  # QR never bypasses authorization
    return qr_response(public_url('patient_history', patient_id=patient_id), 'patient_history_qr.svg')


def generate_patient_history_pdf(patient_id):
    patient, history = patient_records(patient_id)
    font_path = os.environ.get('PDF_FONT_PATH', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if 'MedLinkUnicode' not in pdfmetrics.getRegisteredFontNames():
        if not Path(font_path).is_file():
            abort(503, description='Шрифт для PDF недоступний. Перевірте налаштування сервера.')
        pdfmetrics.registerFont(TTFont('MedLinkUnicode', font_path))
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = 'MedLinkUnicode'
    stream = io.BytesIO()
    document = SimpleDocTemplate(stream, pagesize=A4)
    story = [Paragraph('Медична історія: ' + escape(patient['name']), styles['Title']), Spacer(1, 12)]
    if history:
        paragraph = lambda text: Paragraph(escape(str(text or 'Не вказано')).replace('\n', '<br/>'), styles['Normal'])
        data = [[paragraph(text) for text in ('Дата', 'Рекомендації', 'Ліки')]]
        for entry in history:
            recommendations, medication, date = entry[:3]
            diagnosis = entry[3] if len(entry) > 3 else None
            details = ('Діагноз: ' + diagnosis + '\n' if diagnosis else '') + (recommendations or '')
            data.append([paragraph(date), paragraph(details), paragraph(medication)])
        table = Table(data, colWidths=[100, 205, 170], repeatRows=1, splitInRow=1)
        table.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), 'MedLinkUnicode'),
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightblue), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('BOTTOMPADDING', (0, 0), (-1, -1), 8)]))
        story.append(table)
    else:
        story.append(Paragraph('Медична історія відсутня.', styles['Normal']))
    document.build(story)
    stream.seek(0)
    return stream


@app.route('/download_patient_history_pdf/<int:patient_id>')
def download_patient_history_pdf(patient_id):
    return send_file(generate_patient_history_pdf(patient_id), mimetype='application/pdf',
                     as_attachment=True, download_name=f'patient_{patient_id}_history.pdf')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('Ви вийшли з облікового запису.', 'success')
    return redirect(url_for('main'), code=303)


@app.route('/livez')
def livez():
    return {'status': 'ok'}, 200


@app.route('/healthz')
def healthz():
    try:
        with get_db().cursor() as cursor:
            cursor.execute('SELECT 1 FROM schema_migrations WHERE version = 5')
            if not cursor.fetchone():
                return {'status': 'unavailable'}, 503
        return {'status': 'ok'}, 200
    except psycopg2.Error:
        return {'status': 'unavailable'}, 503


@app.cli.command('create-doctor')
@click.option('--name', prompt='ПІБ лікаря')
@click.option('--email', prompt='Email')
@click.option('--phone', prompt='Телефон')
@click.option('--rnokpp', prompt='РНОКПП')
@click.option('--experience', type=click.IntRange(0,80), prompt='Досвід (років)')
@click.option('--hospital-id', type=click.IntRange(min=1), prompt='ID лікарні')
@click.option('--specialization-id', type=click.IntRange(min=1), prompt='ID спеціалізації')
@click.password_option(prompt='Пароль (12–128 символів)')
def create_doctor(name,email,phone,rnokpp,experience,hospital_id,specialization_id,password):
    """Admin-only CLI provisioning; never creates a public privilege-grant route."""
    email=email.strip().lower()
    if not valid_email(email) or not 2 <= len(name.strip()) <= 150 or not 5 <= len(phone.strip()) <= 30 or not 12 <= len(password) <= 128 or len(rnokpp) != 10 or not rnokpp.isascii() or not rnokpp.isdigit():
        raise click.ClickException('Перевірте дані лікаря та довжину пароля.')
    conn=get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""INSERT INTO doctor(full_name,email,phone,rnokpp,experience,hospital_id,specialization_id,password)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",(name.strip(),email,phone.strip(),rnokpp,experience,hospital_id,specialization_id,generate_password_hash(password,method=PASSWORD_METHOD)))
            id=cursor.fetchone()[0]
            cursor.execute("""INSERT INTO doctor_working_hours(doctor_id,weekday,starts_at,ends_at)
                SELECT %s,day,'09:00'::time,'17:00'::time FROM generate_series(0,4) day""",(id,))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        raise click.ClickException('Конфлікт email/РНОКПП або некоректна лікарня/спеціалізація.') from None
    click.echo('Обліковий запис лікаря створено. Вхід: /doctor/login')


if __name__ == '__main__':
    init_db()
    app.run(debug=False, host='127.0.0.1', port=5000)
