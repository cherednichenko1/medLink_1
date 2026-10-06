"""Real PostgreSQL regressions; TEST_DATABASE_URL must point to a disposable test DB.
Each class creates its own unique schema and never uses the application's DB settings.
"""
import os
import unittest
import uuid
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import psycopg2
from psycopg2 import sql
from werkzeug.security import check_password_hash, generate_password_hash

os.environ.setdefault('FLASK_SECRET_KEY', 'test-only-secret-at-least-thirty-two-characters-long')
import db
from app import app, PASSWORD_METHOD
from security import now_local

TEST_URL = os.environ.get('TEST_DATABASE_URL')


@unittest.skipUnless(TEST_URL, 'TEST_DATABASE_URL not set; real PostgreSQL tests require a test database')
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'medlink_test_' + uuid.uuid4().hex
        cls.original_config = db.DB_CONFIG.copy()
        with closing(psycopg2.connect(TEST_URL)) as conn, conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        db.DB_CONFIG = {'dsn': TEST_URL, 'options': f'-c search_path={cls.schema} -c statement_timeout=15000'}
        db.init_db()
        cls.password = 'correct password with spaces '
        cls.password_hash = generate_password_hash(cls.password, method=PASSWORD_METHOD)
        app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        db.DB_CONFIG = cls.original_config
        with closing(psycopg2.connect(TEST_URL)) as conn, conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        with closing(db.get_db_connection()) as conn, conn, conn.cursor() as cursor:
            cursor.execute('TRUNCATE appointments, patient_history, doctor, "user", login_attempts RESTART IDENTITY CASCADE')
            cursor.execute('''INSERT INTO "user" (name,email,phone,password) VALUES
                ('Пацієнт','patient@example.com','+380991234567',%s),
                ('Інший','other@example.com','+380991234568',%s)''', (self.password_hash, self.password_hash))
            cursor.execute('SELECT id FROM hospital ORDER BY id LIMIT 1')
            hospital = cursor.fetchone()[0]
            cursor.execute('SELECT id FROM specialization ORDER BY id LIMIT 1')
            specialization = cursor.fetchone()[0]
            cursor.execute('''INSERT INTO doctor (full_name,email,rnokpp,phone,experience,hospital_id,specialization_id,password)
                VALUES ('Лікар','doctor@example.com','1234567890','+380991234569',5,%s,%s,%s),
                ('Інший лікар','otherdoctor@example.com','1234567891','+380991234570',5,%s,%s,%s)''',
                (hospital, specialization, self.password_hash, hospital, specialization, self.password_hash))
        with closing(db.get_db_connection()) as conn, conn, conn.cursor() as cursor:
            cursor.execute("INSERT INTO doctor_working_hours(doctor_id,weekday,starts_at,ends_at) SELECT id,day,'09:00'::time,'17:00'::time FROM doctor CROSS JOIN generate_series(0,4) day")
        self.client = app.test_client()
        day = now_local().date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        self.day = day.isoformat()

    def csrf(self, client=None):
        client = client or self.client
        client.get('/loginPage')
        with client.session_transaction() as session:
            return session['_csrf']

    def authenticate(self, role='user', account_id=1, client=None):
        client = client or self.client
        token = self.csrf(client)
        with client.session_transaction() as session:
            session.update(user_role=role, user_id=account_id)
        return token

    def book(self, client=None, account_id=1):
        client = client or self.client
        token = self.authenticate(client=client, account_id=account_id)
        return client.post('/searchPage', data={'csrf_token': token, 'doctor_id': '1',
            'appointment_date': self.day, 'appointment_time': '09:00'})

    def test_registration_hashes_password_and_login_preserves_spaces(self):
        token = self.csrf()
        response = self.client.post('/registerPage', data={'csrf_token': token, 'role': 'user',
            'name': 'Новий пацієнт', 'email': 'NEW@EXAMPLE.COM', 'phone': '+380991234571',
            'password': self.password, 'confirm_password': self.password})
        self.assertEqual(response.status_code, 303)
        with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT password FROM "user" WHERE email = %s', ('new@example.com',))
            stored = cursor.fetchone()[0]
        self.assertNotEqual(stored, self.password)
        self.assertTrue(check_password_hash(stored, self.password))
        response = self.client.post('/getLogin', data={'csrf_token': token, 'role': 'user',
            'enterLogin': 'NEW@EXAMPLE.COM', 'enterPassword': self.password})
        self.assertEqual(response.status_code, 303)
        with self.client.session_transaction() as session:
            self.assertEqual(session['user_role'], 'user')
            self.assertNotEqual(session['_csrf'], token)

    def test_public_doctor_registration_and_login(self):
        token = self.csrf()
        with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT id FROM hospital ORDER BY id LIMIT 1'); hospital = cursor.fetchone()[0]
            cursor.execute('SELECT id FROM specialization ORDER BY id LIMIT 1'); specialization = cursor.fetchone()[0]
        response = self.client.post('/registerPage', data={'csrf_token': token, 'role': 'doctor',
            'name': 'Новий лікар', 'email': 'newdoctor@example.com', 'phone': '+380991234571',
            'password': self.password, 'confirm_password': self.password, 'rnokpp': '9876543210',
            'experience': '5', 'hospital': hospital, 'specialization': specialization})
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.location.endswith('/doctor/login'))
        with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT id,password FROM doctor WHERE email='newdoctor@example.com'")
            doctor_id, hashed = cursor.fetchone()
            self.assertTrue(check_password_hash(hashed,self.password))
            cursor.execute('SELECT COUNT(*) FROM doctor_working_hours WHERE doctor_id=%s',(doctor_id,))
            self.assertEqual(cursor.fetchone()[0],5)
        response = self.client.post('/getLogin', data={'csrf_token': token,'role':'doctor',
            'enterLogin':'newdoctor@example.com','enterPassword':self.password})
        self.assertEqual(response.status_code,303)
        with self.client.session_transaction() as session:
            self.assertEqual(session['user_role'],'doctor')

    def test_qr_mobile_history_login_and_write_permissions(self):
        self.assertEqual(self.client.get('/doctor/patients/1/history').status_code,302)
        self.assertIn('/doctor/login?next=',self.client.get('/doctor/patients/1/history').location)
        self.book()
        token = self.authenticate(role='doctor')
        self.assertEqual(self.client.get('/doctor/patients/1/history').status_code,200)
        data={'csrf_token':token,'diagnosis':'Тестовий діагноз','recommendations':'Рекомендації','medication':'Ліки'}
        self.assertEqual(self.client.post('/doctor/patients/1/history',data=data).status_code,303)
        with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT diagnosis,doctor_id FROM patient_history WHERE patient_id=1')
            self.assertEqual(cursor.fetchone(),('Тестовий діагноз',1))
        self.authenticate(role='doctor',account_id=2)
        self.assertEqual(self.client.get('/doctor/patients/1/history').status_code,403)
        data['csrf_token']=self.authenticate(role='user')
        self.assertEqual(self.client.post('/doctor/patients/1/history',data=data).status_code,403)
        self.assertEqual(self.client.post('/doctor/patients/1/history',data={}).status_code,400)

    def test_qr_mobile_history_login_returns_to_patient(self):
        self.book()
        self.client.post('/logout',data={'csrf_token':self.csrf()})
        destination='/doctor/patients/1/history'
        token=self.csrf()
        response=self.client.post('/getLogin',data={'csrf_token':token,'role':'doctor',
            'enterLogin':'doctor@example.com','enterPassword':self.password,'next':destination})
        self.assertEqual(response.location,destination)

    def test_duplicate_booking_is_rejected_and_cancellation_releases_slot(self):
        self.assertEqual(self.book().status_code, 303)
        other = app.test_client()
        token = self.authenticate(account_id=2, client=other)
        duplicate = other.post('/searchPage', data={'csrf_token': token, 'doctor_id': '1',
            'appointment_date': self.day, 'appointment_time': '09:00'}, follow_redirects=True)
        self.assertIn('Цей час уже зайнятий'.encode(), duplicate.data)
        with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT id FROM appointments')
            appointment_id = cursor.fetchone()[0]
        self.assertEqual(other.post(f'/appointments/{appointment_id}/cancel', data={'csrf_token': token}).status_code, 404)
        token = self.csrf()
        self.assertEqual(self.client.post(f'/appointments/{appointment_id}/cancel', data={'csrf_token': token}).status_code, 303)
        self.assertEqual(self.book(other, account_id=2).status_code, 303)
        with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM appointments WHERE status = 'scheduled'")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_patient_and_unrelated_doctor_cannot_read_or_write_another_patient(self):
        token = self.authenticate()
        self.assertEqual(self.client.get('/user_info/2').status_code, 403)
        self.assertEqual(self.client.get('/patient_history/2').status_code, 403)
        token = self.authenticate('doctor', 2)
        for url in ('/user_info/1', '/patient_history/1', '/download_patient_history_pdf/1', '/generate_qr_patient_history/1', '/doctorCabinet?patient_id=1'):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post('/doctorCabinet', data={'csrf_token': token, 'patient_id': '1', 'recommendations': 'unauthorized'}).status_code, 403)

    def test_patient_doctor_history_flow(self):
        self.book()
        token = self.authenticate('doctor', 1)
        response = self.client.post('/doctorCabinet', data={'csrf_token': token, 'patient_id': '1',
            'recommendations': 'Відпочинок <script>alert(1)</script>', 'medication': 'За призначенням'})
        self.assertEqual(response.status_code, 303)
        self.authenticate('user', 1)
        response = self.client.get('/patient_history/1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Відпочинок'.encode(), response.data)
        self.assertNotIn(b'<script>alert(1)</script>', response.data)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertEqual(self.client.get('/userCabinet').status_code, 200)
        self.assertEqual(self.client.get('/generate_qr_user').status_code, 200)
        self.assertEqual(self.client.get('/healthz').status_code, 200)

    def test_bad_login_is_rate_limited_across_clients(self):
        for index in range(11):
            client = app.test_client()
            token = self.csrf(client)
            response = client.post('/getLogin', data={'csrf_token': token, 'role': 'user',
                'enterLogin': 'patient@example.com', 'enterPassword': 'incorrect password'})
            self.assertEqual(response.status_code, 429 if index == 10 else 303)

    def test_migration_idempotency(self):
        db.init_db()
        db.init_db()
        with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT password FROM "user" WHERE id = 1')
            self.assertEqual(cursor.fetchone()[0], self.password_hash)
            cursor.execute('SELECT COUNT(*) FROM schema_migrations WHERE version = 1')
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_unique_index_prevents_direct_database_double_booking(self):
        with closing(db.get_db_connection()) as conn:
            with conn, conn.cursor() as cursor:
                cursor.execute('INSERT INTO appointments (doctor_id,user_id,appointment_date,appointment_time) VALUES (1,1,%s,%s)', (self.day, '09:00'))
            with self.assertRaises(psycopg2.errors.UniqueViolation), conn, conn.cursor() as cursor:
                cursor.execute('INSERT INTO appointments (doctor_id,user_id,appointment_date,appointment_time) VALUES (1,2,%s,%s)', (self.day, '09:00'))

    def test_doctor_login_and_calendar(self):
        token=self.csrf()
        response=self.client.post('/getLogin',data={'csrf_token':token,'role':'doctor','enterLogin':'doctor@example.com','enterPassword':self.password})
        self.assertEqual(response.status_code,303)
        self.assertIn('/doctorCabinet',response.location)
        self.assertEqual(self.client.get('/doctor/appointments').status_code,200)
        self.assertEqual(self.client.get('/doctor/schedule').status_code,200)

    def test_patient_cannot_use_doctor_processes(self):
        token=self.authenticate()
        for url in ('/doctor/schedule','/doctor/appointments','/doctor/appointments/1'):
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.post('/doctor/schedule',data={'csrf_token':token}).status_code,403)

    def today_visit(self):
        with closing(db.get_db_connection()) as conn,conn,conn.cursor() as cursor:
            cursor.execute("INSERT INTO appointments(doctor_id,user_id,appointment_date,appointment_time) VALUES(1,1,%s,'00:00') RETURNING id",(now_local().date(),))
            return cursor.fetchone()[0]

    def test_doctor_start_complete_and_single_medical_record(self):
        id=self.today_visit()
        token=self.authenticate('doctor',1)
        self.assertEqual(self.client.post(f'/doctor/appointments/{id}',data={'csrf_token':token,'action':'start'}).status_code,303)
        form={'csrf_token':token,'action':'complete','diagnosis':'Тестовий діагноз','recommendations':'Відпочинок','medication':'Не призначено'}
        self.assertEqual(self.client.post(f'/doctor/appointments/{id}',data=form).status_code,303)
        self.assertEqual(self.client.post(f'/doctor/appointments/{id}',data=form).status_code,409)
        with closing(db.get_db_connection()) as conn,conn.cursor() as cursor:
            cursor.execute('SELECT status FROM appointments WHERE id=%s',(id,))
            self.assertEqual(cursor.fetchone()[0],'completed')
            cursor.execute('SELECT diagnosis,COUNT(*) OVER() FROM patient_history WHERE appointment_id=%s',(id,))
            self.assertEqual(cursor.fetchone(),('Тестовий діагноз',1))
        self.authenticate('user',1)
        self.assertIn('Тестовий діагноз'.encode(),self.client.get('/patient_history/1').data)

    def test_doctor_cannot_manage_other_doctors_visit(self):
        id=self.today_visit()
        token=self.authenticate('doctor',2)
        self.assertEqual(self.client.get(f'/doctor/appointments/{id}').status_code,404)
        self.assertEqual(self.client.post(f'/doctor/appointments/{id}',data={'csrf_token':token,'action':'start'}).status_code,404)

    def test_schedule_controls_new_bookings_and_preserves_existing(self):
        self.book()
        token=self.authenticate('doctor',1)
        self.assertEqual(self.client.post('/doctor/schedule',data={'csrf_token':token}).status_code,303)
        with closing(db.get_db_connection()) as conn,conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM appointments WHERE status='scheduled'")
            self.assertEqual(cursor.fetchone()[0],1)
        token=self.authenticate('user',2)
        response=self.client.post('/searchPage',data={'csrf_token':token,'doctor_id':'1','appointment_date':self.day,'appointment_time':'10:00'},follow_redirects=True)
        self.assertIn('Лікар не працює'.encode(),response.data)

    def test_cancelled_or_unstarted_visit_cannot_be_completed(self):
        id=self.today_visit()
        token=self.authenticate('doctor',1)
        form={'csrf_token':token,'action':'complete','diagnosis':'Тест','recommendations':'Тест'}
        self.assertEqual(self.client.post(f'/doctor/appointments/{id}',data=form).status_code,409)
        self.assertEqual(self.client.post(f'/doctor/appointments/{id}',data={'csrf_token':token,'action':'cancel'}).status_code,303)
        self.assertEqual(self.client.post(f'/doctor/appointments/{id}',data={'csrf_token':token,'action':'start'}).status_code,409)


@unittest.skipUnless(TEST_URL, 'TEST_DATABASE_URL not set; migration test requires real PostgreSQL')
class LegacyMigrationTests(unittest.TestCase):
    def test_plaintext_password_migration_and_rollback_on_duplicate_slots(self):
        schema = 'medlink_legacy_' + uuid.uuid4().hex
        original = db.DB_CONFIG.copy()
        try:
            with closing(psycopg2.connect(TEST_URL)) as conn, conn, conn.cursor() as cursor:
                cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            db.DB_CONFIG = {'dsn': TEST_URL, 'options': f'-c search_path={schema}'}
            legacy_password = 'pbkdf2:sha256:1000000$looks$like-a-hash'
            with closing(db.get_db_connection()) as conn, conn, conn.cursor() as cursor:
                cursor.execute('''CREATE TABLE "user" (id SERIAL PRIMARY KEY, name TEXT NOT NULL,
                    email TEXT UNIQUE, phone TEXT, password TEXT)''')
                cursor.execute('INSERT INTO "user" (name,email,password) VALUES (%s,%s,%s)', ('Старий', 'OLD@EXAMPLE.COM', legacy_password))
                cursor.execute('''CREATE TABLE appointments (id SERIAL PRIMARY KEY, doctor_id INTEGER NOT NULL,
                    user_id INTEGER, email TEXT, appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL)''')
                cursor.execute("INSERT INTO appointments (doctor_id,appointment_date,appointment_time) VALUES (1,'2026-10-01','09:00'), (1,'2026-10-01','09:00:00')")
            with self.assertRaises(psycopg2.errors.UniqueViolation):
                db.init_db()
            with closing(db.get_db_connection()) as conn, conn, conn.cursor() as cursor:
                cursor.execute('SELECT password FROM "user"')
                self.assertEqual(cursor.fetchone()[0], legacy_password)
                cursor.execute('SELECT COUNT(*) FROM appointments')
                self.assertEqual(cursor.fetchone()[0], 2)
                # Test-only deliberate correction; production migration never deletes data.
                cursor.execute('DELETE FROM appointments WHERE id = 2')
            db.init_db()
            with closing(db.get_db_connection()) as conn, conn.cursor() as cursor:
                cursor.execute('SELECT email,password FROM "user"')
                email, hashed = cursor.fetchone()
                self.assertEqual(email, 'old@example.com')
                self.assertTrue(check_password_hash(hashed, legacy_password))
        finally:
            db.DB_CONFIG = original
            with closing(psycopg2.connect(TEST_URL)) as conn, conn, conn.cursor() as cursor:
                cursor.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
