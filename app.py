import io
import os
import secrets
from datetime import timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from urllib.parse import urlsplit

import psycopg2
import qrcode
from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from qrcode.image.svg import SvgPathImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
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
app.teardown_appcontext(close_db)
app.before_request(protect_csrf)
app.jinja_env.globals['csrf_token'] = csrf_token
app.jinja_env.filters['strftime'] = lambda dt, fmt: now_local().strftime(fmt) if dt == 'now' else dt.strftime(fmt)
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'http://localhost:8080').rstrip('/')
parsed_public_url = urlsplit(PUBLIC_BASE_URL)
if parsed_public_url.scheme not in {'http', 'https'} or not parsed_public_url.netloc or parsed_public_url.query or parsed_public_url.fragment or parsed_public_url.username:
    raise RuntimeError('PUBLIC_BASE_URL має бути адресою http(s) без облікових даних, query або fragment.')
PASSWORD_METHOD = 'pbkdf2:sha256:1000000'
# Equivalent work for unknown accounts to reduce account enumeration by timing.
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32), method=PASSWORD_METHOD)


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
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
    return render_template('base.html')


@app.route('/helsiPage')
def helsiPage():
    return render_template('medlink.html')


@app.route('/loginPage')
def loginPage():
    return render_template('login.html')


def registration_form(error=None, status=200):
    with get_db().cursor() as cursor:
        cursor.execute('SELECT id, name FROM hospital ORDER BY name')
        hospitals = cursor.fetchall()
        cursor.execute('SELECT id, name FROM specialization ORDER BY name')
        specializations = cursor.fetchall()
    return render_template('registerForm.html', hospitals=hospitals, specializations=specializations,
                           error=error, values=request.form, doctor_registration_enabled=bool(os.environ.get('DOCTOR_REGISTRATION_CODE'))), status


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
        invite = os.environ.get('DOCTOR_REGISTRATION_CODE', '')
        if not invite or not secrets.compare_digest(invite.encode(), request.form.get('doctor_code', '').encode()):
            return registration_form('Потрібен дійсний код запрошення від адміністратора.', 403)
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
                    hospital_id, specialization_id, password) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''', params)
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        return registration_form('Не вдалося зареєструватися. Перевірте дані або зверніться до адміністратора.', 400)
    flash('Обліковий запис створено. Тепер увійдіть.', 'success')
    return redirect(url_for('loginPage'), code=303)


@app.route('/getLogin', methods=['POST'])
def get_login():
    role = request.form.get('role')
    login = request.form.get('enterLogin', '').strip().lower()
    password = request.form.get('enterPassword', '')
    if role not in {'user', 'doctor'} or not login or len(login) > 254 or not 1 <= len(password) <= 128:
        flash('Перевірте роль, логін і пароль.', 'error')
        return redirect(url_for('loginPage'), code=303)
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
        return redirect(url_for('loginPage'), code=303)
    session.clear()
    session.permanent = True
    session.update(user_role=role, user_id=row[0])
    csrf_token()
    flash('Вхід виконано.', 'success')
    return redirect(url_for('doctor_cabinet' if role == 'doctor' else 'user_cabinet'), code=303)


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
            WHERE doctor_id = ANY(%s) AND status = 'scheduled' AND appointment_date >= %s
            ORDER BY appointment_date, appointment_time''', ([d[0] for d in doctors], now_local().date()))
        doctor_appointments = {}
        for doctor_id, date, time in cursor.fetchall():
            doctor_appointments.setdefault(doctor_id, []).append((date, time))
    today = now_local().date()
    return render_template('searchForm.html', doctors=doctors, hospital_types=hospital_types,
        districts=districts, specializations=specializations, doctor_appointments=doctor_appointments,
        page=page, has_next=has_next, today=today.isoformat(), max_date=(today + timedelta(days=90)).isoformat(), filters=request.args, next_url=url_for('searchPage', **dict(request.args.to_dict(), page=page + 1)),
        previous_url=url_for('searchPage', **dict(request.args.to_dict(), page=max(1, page - 1))))


def book_appointment():
    require_role('user')
    doctor_id = positive_id(request.form.get('doctor_id'))
    try:
        date, time = parse_slot(request.form.get('appointment_date'), request.form.get('appointment_time'))
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('searchPage'), code=303)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
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
        return redirect(url_for('loginPage'))
    patient_value = request.form.get('patient_id') if request.method == 'POST' else request.args.get('patient_id')
    patient_id = positive_id(patient_value) if patient_value else None
    conn = get_db()
    with conn.cursor() as cursor:
        doctor = doctor_record(cursor, session['user_id'])
        cursor.execute('''SELECT u.id, u.name FROM "user" u WHERE EXISTS (
            SELECT 1 FROM appointments a WHERE a.user_id = u.id AND a.doctor_id = %s AND a.status = 'scheduled'
        ) OR EXISTS (SELECT 1 FROM patient_history ph WHERE ph.patient_id = u.id AND ph.doctor_id = %s)
        ORDER BY u.name''', (session['user_id'], session['user_id']))
        patients = cursor.fetchall()
        cursor.execute('''SELECT a.appointment_date, a.appointment_time, u.name FROM appointments a
            LEFT JOIN "user" u ON u.id = a.user_id WHERE a.doctor_id = %s AND a.status = 'scheduled'
            AND (a.appointment_date + a.appointment_time) > %s ORDER BY a.appointment_date, a.appointment_time''',
            (session['user_id'], now_local().replace(tzinfo=None)))
        appointments = cursor.fetchall()
        history = []
        if patient_id:
            require_patient_access(cursor, patient_id)
            cursor.execute('''SELECT patient_id, recommendations, medication, timestamp FROM patient_history
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
        abort(403, description='Для перегляду персональних даних потрібно увійти.')
    with get_db().cursor() as cursor:
        require_patient_access(cursor, user_id)
        cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (user_id,))
        row = cursor.fetchone()
        if not row:
            abort(404)
    return render_template('user_info.html', user=dict(zip(('name', 'email', 'phone'), row)))


def patient_records(patient_id):
    if session.get('user_role') not in {'user', 'doctor'}:
        abort(403, description='Для перегляду медичної історії потрібно увійти.')
    with get_db().cursor() as cursor:
        require_patient_access(cursor, patient_id)
        cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (patient_id,))
        row = cursor.fetchone()
        if not row:
            abort(404)
        query = 'SELECT recommendations, medication, timestamp FROM patient_history WHERE patient_id = %s'
        params = [patient_id]
        if session['user_role'] == 'doctor':
            query += ' AND doctor_id = %s'
            params.append(session['user_id'])
        cursor.execute(query + ' ORDER BY timestamp DESC', params)
        history = cursor.fetchall()
    return dict(zip(('name', 'email', 'phone'), row)), history


@app.route('/patient_history/<int:patient_id>')
def patient_history(patient_id):
    patient, history = patient_records(patient_id)
    return render_template('patient_history.html', patient=patient, history=history)


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
    return send_file(stream, mimetype='image/svg+xml', as_attachment=True, download_name=filename)


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
        data += [[paragraph(date), paragraph(recommendations), paragraph(medication)] for recommendations, medication, date in history]
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
            cursor.execute('SELECT 1 FROM schema_migrations WHERE version = 1')
            if not cursor.fetchone():
                return {'status': 'unavailable'}, 503
        return {'status': 'ok'}, 200
    except psycopg2.Error:
        return {'status': 'unavailable'}, 503


if __name__ == '__main__':
    init_db()
    app.run(debug=False, host='127.0.0.1', port=5000)
