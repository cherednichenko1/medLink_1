import os
import io
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
import qrcode
from qrcode.image.svg import SvgPathImage
from flask import Flask, render_template, request, session, redirect, url_for, send_file
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'your_super_secret_key_here')

app.jinja_env.filters['strftime'] = lambda dt, fmt: datetime.now().strftime(fmt) if dt == 'now' else dt.strftime(fmt)

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': os.environ.get('DB_PORT', '5432'),
    'dbname': os.environ.get('DB_NAME', 'medlink'),
    'user': os.environ.get('DB_USER', 'medlink'),
    'password': os.environ.get('DB_PASSWORD', 'medlink'),
}


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def wait_for_db(max_retries=30, delay=2):
    """Чекаємо, поки Postgres підніметься (важливо для k8s, де контейнери стартують паралельно)."""
    for attempt in range(1, max_retries + 1):
        try:
            conn = get_db_connection()
            conn.close()
            print("З'єднання з базою даних успішне.")
            return
        except psycopg2.OperationalError as e:
            print(f"Спроба {attempt}/{max_retries}: база даних ще не готова ({e}). Чекаю {delay}с...")
            time.sleep(delay)
    raise RuntimeError("Не вдалося підключитися до бази даних після кількох спроб.")


def init_db():
    wait_for_db()
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS district (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS hospital (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        district_id INTEGER NOT NULL REFERENCES district(id),
        edrpou TEXT NOT NULL,
        address TEXT
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS specialization (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS doctor (
        id SERIAL PRIMARY KEY,
        full_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        experience INTEGER NOT NULL,
        hospital_id INTEGER NOT NULL REFERENCES hospital(id),
        specialization_id INTEGER NOT NULL REFERENCES specialization(id),
        rnokpp TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS "user" (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        phone TEXT,
        password TEXT
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS patient_history (
        id SERIAL PRIMARY KEY,
        patient_id INTEGER NOT NULL REFERENCES "user"(id),
        doctor_id INTEGER NOT NULL REFERENCES doctor(id),
        recommendations TEXT,
        medication TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Аналог PRAGMA table_info + перейменування колонки зі старого main.py/app2.py
    cursor.execute('''
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'patient_history'
    ''')
    columns = [row[0] for row in cursor.fetchall()]
    if 'diagnosis' in columns and 'recommendations' not in columns:
        cursor.execute('ALTER TABLE patient_history RENAME COLUMN diagnosis TO recommendations')
        print("Колонка 'diagnosis' перейменована на 'recommendations'.")

    cursor.execute('''CREATE TABLE IF NOT EXISTS appointments (
        id SERIAL PRIMARY KEY,
        doctor_id INTEGER NOT NULL REFERENCES doctor(id),
        user_id INTEGER REFERENCES "user"(id),
        email TEXT,
        appointment_date TEXT NOT NULL,
        appointment_time TEXT NOT NULL
    )''')

    cursor.execute("SELECT COUNT(*) FROM hospital")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO district (name) VALUES ('Тестовий район')")
        cursor.execute('''INSERT INTO hospital (name, type, district_id, edrpou, address)
                          VALUES ('Тестова лікарня', 'Державна', 1, '12345678', 'вул. Тестова, 1')''')
        print("Додано тестову лікарню")

    cursor.execute("SELECT COUNT(*) FROM specialization")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO specialization (name) VALUES ('Терапевт')")
        print("Додано тестову спеціалізацію")

    conn.commit()
    cursor.close()
    conn.close()


def generate_patient_history_pdf(patient_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT name FROM "user" WHERE id = %s', (patient_id,))
    patient_name = cursor.fetchone()[0]

    cursor.execute('''SELECT recommendations, medication, timestamp
                      FROM patient_history
                      WHERE patient_id = %s AND doctor_id = %s
                      ORDER BY timestamp DESC''', (patient_id, session.get('user_id')))
    history = cursor.fetchall()

    cursor.close()
    conn.close()

    pdf_io = io.BytesIO()
    doc = SimpleDocTemplate(pdf_io, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Історія хвороб пацієнта: {patient_name}", styles['Title']))
    story.append(Spacer(1, 12))

    if history:
        data = [['Дата', 'Рекомендації', 'Ліки']]
        for entry in history:
            data.append([str(entry[2]), entry[0] or 'Не вказано', entry[1] or 'Не вказано'])

        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("Історія хвороб відсутня.", styles['Normal']))

    doc.build(story)
    pdf_io.seek(0)
    return pdf_io


@app.route('/')
def main() -> 'html':
    return render_template('base.html', session=session)


@app.route('/helsiPage')
def helsiPage() -> 'html':
    return render_template('medlink.html', session=session)


@app.route('/loginPage')
def loginPage() -> 'html':
    return render_template('login.html', session=session)


@app.route('/registerPage', methods=['GET', 'POST'])
def registerPage():
    if request.method == 'POST':
        role = request.form.get('role')
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        phone = request.form.get('phone')
        rnokpp = request.form.get('rnokpp')
        experience = request.form.get('experience')
        hospital_id = request.form.get('hospital')
        specialization_id = request.form.get('specialization')

        conn = get_db_connection()
        cursor = conn.cursor()

        if role == 'user':
            try:
                cursor.execute('''INSERT INTO "user" (name, email, phone, password)
                                  VALUES (%s, %s, %s, %s)''', (name, email, phone, password))
                conn.commit()
                cursor.close()
                conn.close()
                print(f"Користувач зареєстрований: {name}, {email}")
                return redirect(url_for('loginPage'))
            except psycopg2.IntegrityError:
                conn.rollback()
                cursor.close()
                conn.close()
                return render_template('registerForm.html', error="Цей email уже зареєстровано", session=session)

        elif role == 'doctor':
            if not all([name, email, rnokpp, phone, experience, hospital_id, specialization_id, password]):
                cursor.close()
                conn.close()
                return render_template('registerForm.html', error="Заповніть усі поля", session=session)
            try:
                cursor.execute('''INSERT INTO doctor (full_name, email, rnokpp, phone, experience, hospital_id, specialization_id, password)
                                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                               (name, email, rnokpp, phone, experience, hospital_id, specialization_id, password))
                conn.commit()
                cursor.close()
                conn.close()
                print(f"Лікар зареєстрований: {name}, {email}")
                return redirect(url_for('loginPage'))
            except psycopg2.IntegrityError:
                conn.rollback()
                cursor.close()
                conn.close()
                return render_template('registerForm.html', error="Цей email або РНОКПП уже зареєстровано", session=session)

        cursor.close()
        conn.close()
        return render_template('registerForm.html', error="Невідома роль", session=session)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM hospital")
    hospitals = cursor.fetchall()
    cursor.execute("SELECT id, name FROM specialization")
    specializations = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template('registerForm.html', hospitals=hospitals, specializations=specializations, session=session)


@app.route('/searchPage', methods=['GET', 'POST'])
def searchPage():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT DISTINCT type FROM hospital")
    hospital_types = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT name FROM district")
    districts = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT name FROM specialization")
    specializations = [row[0] for row in cursor.fetchall()]

    query = '''SELECT doctor.id, doctor.full_name, doctor.phone, doctor.experience, hospital.name, hospital.type,
                      h_district.name, hospital.address, hospital.edrpou, specialization.name, doctor.email
               FROM doctor
               JOIN hospital ON doctor.hospital_id = hospital.id
               JOIN district AS h_district ON hospital.district_id = h_district.id
               JOIN specialization ON doctor.specialization_id = specialization.id
               WHERE 1=1'''
    params = []

    if request.method == 'POST' and 'experience' in request.form:
        experience = request.form.get('experience')
        hospital_type = request.form.get('hospital_type')
        district = request.form.get('district')
        specialization = request.form.get('specialization')

        if experience:
            query += ' AND doctor.experience >= %s'
            params.append(experience)

        if hospital_type:
            query += ' AND hospital.type = %s'
            params.append(hospital_type)

        if district:
            query += ' AND h_district.name = %s'
            params.append(district)

        if specialization:
            query += ' AND specialization.name = %s'
            params.append(specialization)

    try:
        cursor.execute(query, params)
        doctors = cursor.fetchall()
    except psycopg2.Error as e:
        conn.rollback()
        cursor.close()
        conn.close()
        return f"Помилка бази даних: {str(e)}", 500

    doctor_appointments = {}

    if request.method == 'POST' and 'doctor_id' in request.form:
        doctor_id = request.form.get('doctor_id')
        appointment_date = request.form.get('appointment_date')
        appointment_time = request.form.get('appointment_time')
        user_id = session.get('user_id')
        email = request.form.get('email') if not user_id else None

        if not doctor_id or not appointment_date or not appointment_time:
            for d in doctors:
                cursor.execute('SELECT appointment_date, appointment_time FROM appointments WHERE doctor_id = %s', (d[0],))
                doctor_appointments[d[0]] = cursor.fetchall()
            cursor.close()
            conn.close()
            return render_template('searchForm.html', doctors=doctors, hospital_types=hospital_types, districts=districts, specializations=specializations, doctor_appointments=doctor_appointments, session=session, error="Заповніть усі поля для запису")

        if not user_id and not email:
            for d in doctors:
                cursor.execute('SELECT appointment_date, appointment_time FROM appointments WHERE doctor_id = %s', (d[0],))
                doctor_appointments[d[0]] = cursor.fetchall()
            cursor.close()
            conn.close()
            return render_template('searchForm.html', doctors=doctors, hospital_types=hospital_types, districts=districts, specializations=specializations, doctor_appointments=doctor_appointments, session=session, error="Вкажіть email для запису")

        cursor.execute('''INSERT INTO appointments (doctor_id, user_id, email, appointment_date, appointment_time)
                          VALUES (%s, %s, %s, %s, %s)''', (doctor_id, user_id, email, appointment_date, appointment_time))
        conn.commit()
        print(f"Запис на прийом: Лікар ID={doctor_id}, Дата={appointment_date}, Час={appointment_time}, User ID={user_id or 'Немає'}, Email={email or 'Немає'}")

    for d in doctors:
        cursor.execute('SELECT appointment_date, appointment_time FROM appointments WHERE doctor_id = %s', (d[0],))
        doctor_appointments[d[0]] = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('searchForm.html', doctors=doctors, hospital_types=hospital_types, districts=districts, specializations=specializations, doctor_appointments=doctor_appointments, session=session)


@app.route('/getLogin', methods=['POST'])
def get_login():
    role = request.form['role']
    login = request.form['enterLogin'].strip()
    password = request.form['enterPassword'].strip()

    conn = get_db_connection()
    cursor = conn.cursor()

    if role == 'doctor':
        cursor.execute('''
            SELECT doctor.id, doctor.full_name, doctor.phone, doctor.experience,
                   doctor.email, specialization.name, hospital.name
            FROM doctor
            JOIN specialization ON doctor.specialization_id = specialization.id
            JOIN hospital ON doctor.hospital_id = hospital.id
            WHERE (doctor.email = %s OR doctor.rnokpp = %s) AND doctor.password = %s
        ''', (login, login, password))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            session['user_role'] = 'doctor'
            session['user_id'] = row[0]
            session['user_name'] = row[1]
            return redirect(url_for('doctor_cabinet'))
        else:
            return render_template("login.html", error="Невірний логін або пароль для лікаря", session=session)

    elif role == 'user':
        cursor.execute('SELECT id, name FROM "user" WHERE email = %s AND password = %s', (login, password))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            session['user_role'] = 'user'
            session['user_id'] = row[0]
            session['user_name'] = row[1]
            return redirect(url_for('user_cabinet'))
        else:
            return render_template("login.html", error="Невірний логін або пароль для користувача", session=session)

    cursor.close()
    conn.close()
    return "Невідома роль", 400


@app.route('/doctorCabinet', methods=['GET', 'POST'])
def doctor_cabinet():
    if session.get('user_role') != 'doctor':
        return redirect(url_for('main'))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''SELECT doctor.full_name, doctor.phone, doctor.experience,
                             doctor.email, specialization.name, hospital.name
                      FROM doctor
                      JOIN specialization ON doctor.specialization_id = specialization.id
                      JOIN hospital ON doctor.hospital_id = hospital.id
                      WHERE doctor.id = %s''', (session['user_id'],))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return "Лікаря не знайдено", 404

    doctor = {
        'full_name': row[0],
        'phone': row[1],
        'experience': row[2],
        'email': row[3],
        'specialization': row[4],
        'hospital': row[5]
    }

    cursor.execute('SELECT id, name FROM "user"')
    patients = cursor.fetchall()

    patient_id = request.args.get('patient_id') or (request.form.get('patient_id') if request.method == 'POST' else None)
    history = []
    if patient_id:
        cursor.execute('''SELECT patient_id, recommendations, medication, timestamp
                          FROM patient_history
                          WHERE doctor_id = %s AND patient_id = %s
                          ORDER BY timestamp DESC''', (session['user_id'], patient_id))
        history = cursor.fetchall()

    if request.method == 'POST' and patient_id:
        recommendations = request.form.get('recommendations')
        medication = request.form.get('medication')
        if recommendations or medication:
            cursor.execute('''INSERT INTO patient_history (patient_id, doctor_id, recommendations, medication)
                              VALUES (%s, %s, %s, %s)''', (patient_id, session['user_id'], recommendations, medication))
            conn.commit()
            cursor.close()
            conn.close()
            return redirect(url_for('doctor_cabinet', patient_id=patient_id))

    cursor.close()
    conn.close()

    return render_template('doctorCabinet.html', doctor=doctor, patients=patients, history=history, selected_patient_id=patient_id, session=session)


@app.route('/userCabinet')
def user_cabinet():
    if session.get('user_role') != 'user':
        return redirect(url_for('main'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (session['user_id'],))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return "Користувача не знайдено", 404

    user = {
        'name': row[0],
        'email': row[1],
        'phone': row[2]
    }

    return render_template('userCabinet.html', user_name=session['user_name'], user=user, session=session)


@app.route('/doctor_info/<email>')
def doctor_info(email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''SELECT doctor.full_name, doctor.phone, doctor.experience,
                             doctor.email, specialization.name, hospital.name
                      FROM doctor
                      JOIN specialization ON doctor.specialization_id = specialization.id
                      JOIN hospital ON doctor.hospital_id = hospital.id
                      WHERE doctor.email = %s''', (email,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return "Лікаря не знайдено", 404

    doctor = {
        'full_name': row[0],
        'phone': row[1],
        'experience': row[2],
        'email': row[3],
        'specialization': row[4],
        'hospital': row[5]
    }

    return render_template('doctor_info.html', doctor=doctor)


@app.route('/user_info/<int:user_id>')
def user_info(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return "Користувача не знайдено", 404

    user = {
        'name': row[0],
        'email': row[1],
        'phone': row[2]
    }

    return render_template('user_info.html', user=user)


@app.route('/patient_history/<int:patient_id>')
def patient_history(patient_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT name, email, phone FROM "user" WHERE id = %s', (patient_id,))
    patient_row = cursor.fetchone()
    if not patient_row:
        cursor.close()
        conn.close()
        return "Пацієнта не знайдено", 404

    patient = {
        'name': patient_row[0],
        'email': patient_row[1],
        'phone': patient_row[2]
    }

    cursor.execute('''SELECT recommendations, medication, timestamp
                      FROM patient_history
                      WHERE patient_id = %s AND doctor_id = %s
                      ORDER BY timestamp DESC''', (patient_id, session.get('user_id')))
    history = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('patient_history.html', patient=patient, history=history)


@app.route('/generate_qr_doctor/<email>')
def generate_qr_doctor(email):
    doctor_url = url_for('doctor_info', email=email, _external=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(doctor_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    img_io = io.BytesIO()
    img.save(img_io)
    img_io.seek(0)
    return send_file(img_io, mimetype='image/svg+xml', as_attachment=True, download_name=f"doctor_qr_{email}.svg")


@app.route('/generate_qr_user')
def generate_qr_user():
    if session.get('user_role') != 'user':
        return redirect(url_for('main'))

    user_url = url_for('user_info', user_id=session['user_id'], _external=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(user_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    img_io = io.BytesIO()
    img.save(img_io)
    img_io.seek(0)
    return send_file(img_io, mimetype='image/svg+xml', as_attachment=True, download_name=f"user_qr_{session['user_id']}.svg")


@app.route('/generate_qr_patient_history/<int:patient_id>')
def generate_qr_patient_history(patient_id):
    if session.get('user_role') != 'doctor':
        return redirect(url_for('main'))

    history_url = url_for('patient_history', patient_id=patient_id, _external=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(history_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    img_io = io.BytesIO()
    img.save(img_io)
    img_io.seek(0)
    return send_file(img_io, mimetype='image/svg+xml', as_attachment=True, download_name=f"patient_history_qr_{patient_id}.svg")


@app.route('/download_patient_history_pdf/<int:patient_id>')
def download_patient_history_pdf(patient_id):
    if session.get('user_role') != 'doctor':
        return redirect(url_for('main'))

    pdf_io = generate_patient_history_pdf(patient_id)
    return send_file(pdf_io, mimetype='application/pdf', as_attachment=True, download_name=f"patient_{patient_id}_history.pdf")


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('main'))


@app.route('/healthz')
def healthz():
    """Простий health-check для Kubernetes liveness/readiness проб."""
    try:
        conn = get_db_connection()
        conn.close()
        return {"status": "ok"}, 200
    except Exception as e:
        return {"status": "error", "detail": str(e)}, 503


# Ініціалізація БД відбувається при імпорті модуля, а не лише в __main__,
# щоб працювало і з gunicorn (production), і з `python app.py` (локально).
init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
