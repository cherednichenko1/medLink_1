import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ.setdefault('FLASK_SECRET_KEY', 'test-only-secret-at-least-thirty-two-characters-long')
from app import app, generate_patient_history_pdf, public_url
from security import LOCAL_TZ, can_access_patient, parse_slot, valid_email


class SecurityTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def token(self, client=None):
        client = client or self.client
        client.get('/loginPage')
        with client.session_transaction() as session:
            return session['_csrf']

    def test_public_pages_and_headers(self):
        for url in ('/', '/helsiPage', '/loginPage', '/livez'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_session_cookie_flags(self):
        response = self.client.get('/loginPage')
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertIn('medlink_session_v2=', cookie)

    def test_request_size_is_bounded(self):
        self.assertEqual(self.client.post('/getLogin', data={'large': 'x' * 70000}).status_code, 413)

    def test_registration_password_validation(self):
        token = self.token()
        with patch('app.registration_form', return_value=('invalid', 400)) as form, patch('app.get_db') as database:
            response = self.client.post('/registerPage', data={'csrf_token': token, 'role': 'user',
                'name': 'Пацієнт', 'email': 'patient@example.com', 'phone': '+380991234567',
                'password': 'short', 'confirm_password': 'short'})
            self.assertEqual(response.status_code, 400)
            form.assert_called_once()
            database.assert_not_called()

    def test_unicode_doctor_identifier_rejected(self):
        token = self.token()
        with patch.dict(os.environ, {'DOCTOR_REGISTRATION_CODE': 'test-invitation'}), patch('app.registration_form', return_value=('invalid', 400)), patch('app.get_db') as database:
            response = self.client.post('/registerPage', data={'csrf_token': token, 'role': 'doctor',
                'name': 'Лікар', 'email': 'doctor@example.com', 'phone': '+380991234567',
                'password': 'long secure password', 'confirm_password': 'long secure password', 'rnokpp': 'кирилиця'})
            self.assertEqual(response.status_code, 400)
            database.assert_not_called()

    def test_all_templates_compile(self):
        for name in app.jinja_env.list_templates():
            app.jinja_env.get_template(name)

    def test_missing_csrf_rejected_before_database_access(self):
        with patch('app.get_db') as database:
            for url in ('/getLogin', '/registerPage', '/searchPage', '/logout', '/doctorCabinet', '/appointments/1/cancel'):
                self.assertEqual(self.client.post(url).status_code, 400)
            database.assert_not_called()

    def test_forged_csrf_rejected(self):
        self.token()
        self.assertEqual(self.client.post('/logout', data={'csrf_token': 'forged'}).status_code, 400)

    def test_unicode_csrf_is_rejected_without_server_error(self):
        self.token()
        self.assertEqual(self.client.post('/logout', data={'csrf_token': 'кирилиця'}).status_code, 400)

    def test_token_from_another_session_rejected(self):
        other = app.test_client()
        token = self.token(other)
        self.token()
        self.assertEqual(self.client.post('/logout', data={'csrf_token': token}).status_code, 400)

    def test_logout_post_clears_authentication(self):
        token = self.token()
        with self.client.session_transaction() as session:
            session.update(user_id=7, user_role='user')
        response = self.client.post('/logout', data={'csrf_token': token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Ви вийшли'.encode(), response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn('user_id', session)

    def test_logout_get_is_not_a_mutation(self):
        with self.client.session_transaction() as session:
            session.update(user_id=7, user_role='user')
        self.assertEqual(self.client.get('/logout').status_code, 405)
        with self.client.session_transaction() as session:
            self.assertEqual(session['user_id'], 7)

    def test_guest_cannot_read_personal_data(self):
        with patch('app.get_db') as database:
            for url in ('/download_patient_history_pdf/1', '/generate_qr_patient_history/1'):
                self.assertEqual(self.client.get(url).status_code, 403)
            database.assert_not_called()

    def test_doctor_cannot_book_as_patient_with_same_numeric_id(self):
        token = self.token()
        with self.client.session_transaction() as session:
            session.update(user_id=1, user_role='doctor')
        with patch('app.get_db') as database:
            self.assertEqual(self.client.post('/searchPage', data={'csrf_token': token, 'doctor_id': '1'}).status_code, 403)
            database.assert_not_called()

    def test_guest_cannot_book(self):
        token = self.token()
        self.assertEqual(self.client.post('/searchPage', data={'csrf_token': token}).status_code, 403)

    def test_patient_access_rules(self):
        cursor = MagicMock()
        with app.test_request_context():
            from flask import session
            session.update(user_role='user', user_id=7)
            self.assertTrue(can_access_patient(cursor, 7))
            self.assertFalse(can_access_patient(cursor, 8))
            cursor.execute.assert_not_called()

    def test_doctor_access_requires_relationship(self):
        cursor = MagicMock()
        with app.test_request_context():
            from flask import session
            session.update(user_role='doctor', user_id=3)
            cursor.fetchone.return_value = (False,)
            self.assertFalse(can_access_patient(cursor, 7))
            cursor.fetchone.return_value = (True,)
            self.assertTrue(can_access_patient(cursor, 7))
            self.assertEqual(cursor.execute.call_args.args[1], (7, 3, 7, 3))

    def test_health_does_not_disclose_database_error(self):
        import psycopg2
        with patch('app.get_db', side_effect=psycopg2.OperationalError('password=SECRET host=internal')):
            response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'SECRET', response.data)

    def test_general_database_error_is_generic(self):
        import psycopg2
        with patch('app.get_db', side_effect=psycopg2.OperationalError('password=SECRET')):
            response = self.client.get('/registerPage')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'SECRET', response.data)

    def test_emails(self):
        self.assertTrue(valid_email('patient@example.com'))
        for email in ('', 'patient', 'a@b', 'a b@example.com', 'x' * 255 + '@example.com'):
            self.assertFalse(valid_email(email))

    def test_future_weekday_slot(self):
        now = datetime(2026, 9, 30, 8, 0, tzinfo=LOCAL_TZ)
        with patch('security.now_local', return_value=now):
            date, time = parse_slot('2026-10-01', '09:30')
            self.assertEqual(str(date), '2026-10-01')
            self.assertEqual(str(time), '09:30:00')

    def test_invalid_slots(self):
        now = datetime(2026, 9, 30, 10, 0, tzinfo=LOCAL_TZ)
        bad = [('2026-09-30', '09:00'), ('2026-10-03', '09:00'), ('2026-10-01', '17:00'),
               ('2026-10-01', '09:15'), ('2026-10-01', '08:30'), ('2027-02-01', '09:00'),
               ('2026-02-30', '09:00'), ('2026-10-1', '09:00'), ('2026-10-01', '9:00'),
               ('2026-10-01', '09:00:00'), (None, None)]
        with patch('security.now_local', return_value=now):
            for date, time in bad:
                with self.subTest(date=date, time=time), self.assertRaises(ValueError):
                    parse_slot(date, time)

    def test_qr_url_not_controlled_by_host_header(self):
        with app.test_request_context(headers={'Host': 'attacker.example'}):
            self.assertNotIn('attacker.example', public_url('user_info', user_id=1))

    def test_unicode_pdf_with_markup_and_long_content(self):
        font = os.environ.get('PDF_FONT_PATH', '/Library/Fonts/Arial Unicode.ttf')
        if not os.path.isfile(font):
            self.skipTest('Set PDF_FONT_PATH to a Unicode TTF font')
        with app.test_request_context(), patch.dict(os.environ, {'PDF_FONT_PATH': font}), patch('app.patient_records', return_value=(
                {'name': 'Іван <Пацієнт> & родина'}, [('Рекомендації <script> & ' + 'а' * 5000, 'Ліки', datetime.now())])):
            pdf = generate_patient_history_pdf(7)
            self.assertTrue(pdf.read().startswith(b'%PDF-'))


if __name__ == '__main__':
    unittest.main()
