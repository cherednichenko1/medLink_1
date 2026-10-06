import os
import time
import unittest
from unittest.mock import MagicMock, patch
os.environ.setdefault('FLASK_SECRET_KEY', 'test-only-secret-at-least-thirty-two-characters-long')
from app import app, share_serializer, decode_share, safe_next


class QrTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def csrf(self):
        self.client.get('/loginPage')
        with self.client.session_transaction() as session:
            return session['_csrf']

    def test_private_mobile_pages_redirect_to_login_with_return(self):
        for url in ['/user_info/1', '/patient_history/1']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('next=', response.location)
            self.assertIn('/loginPage', response.location)

    def test_return_target_has_no_open_redirect(self):
        for value in ['https://evil.test', '//evil.test', '/logout', '/user_info/1?next=evil', '/user_info/1\\evil']:
            self.assertEqual(safe_next(value), '')
        self.assertEqual(safe_next('/user_info/1'), '/user_info/1')
        self.assertEqual(safe_next('/patient_history/7'), '/patient_history/7')

    def test_qr_svg_is_inline(self):
        response = self.client.get('/generate_qr_doctor/doctor@example.com')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['Content-Disposition'].startswith('inline'))
        self.assertEqual(response.mimetype, 'image/svg+xml')

    def test_guest_cannot_issue_share(self):
        token = self.csrf()
        with patch('app.get_db') as database:
            self.assertEqual(self.client.post('/qr/share', data={'csrf_token': token, 'scope':'profile', 'patient_id':'1'}).status_code, 403)
            database.assert_not_called()

    def test_profile_owner_must_explicitly_post_and_cannot_share_another_patient(self):
        token = self.csrf()
        with self.client.session_transaction() as session:
            session.update(user_role='user', user_id=1)
        self.assertEqual(self.client.get('/qr/share').status_code, 405)
        self.assertEqual(self.client.post('/qr/share', data={'scope':'profile', 'patient_id':'1'}).status_code, 400)
        self.assertEqual(self.client.post('/qr/share', data={'csrf_token':token,'scope':'profile','patient_id':'2'}).status_code, 403)
        response = self.client.post('/qr/share', data={'csrf_token':token,'scope':'profile','patient_id':'1'})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['expires_in'], 900)
        self.assertIn('/shared#',data['url'])
        self.assertTrue(data['image'].startswith('data:image/svg+xml;base64,'))
        with app.test_request_context():
            payload = decode_share(data['url'].split('#')[1])
            self.assertEqual(payload['patient_id'],1)
            self.assertEqual(payload['scope'],'profile')

    def test_tampered_token_rejected(self):
        self.assertEqual(self.client.post('/shared',data={'csrf_token':self.csrf(),'token':'forged'}).status_code,403)

    def test_expired_token_rejected(self):
        with app.test_request_context(), patch('itsdangerous.timed.TimestampSigner.get_timestamp',return_value=int(time.time())-901):
            token=share_serializer().dumps({'v':1,'scope':'profile','patient_id':1,'doctor_id':None})
        csrf=self.csrf()
        self.assertEqual(self.client.post('/shared',data={'csrf_token':csrf,'token':token}).status_code,403)

    def test_shared_profile_can_be_opened_on_guest_phone(self):
        with app.test_request_context():
            token=share_serializer().dumps({'v':1,'scope':'profile','patient_id':1,'doctor_id':None})
        conn=MagicMock();cursor=conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=('Пацієнт','patient@example.com','+38099')
        with patch('app.get_db',return_value=conn):
            response=self.client.post('/shared',data={'csrf_token':self.csrf(),'token':token})
        self.assertEqual(response.status_code,200)
        self.assertIn('Пацієнт'.encode(),response.data)
        self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_doctor_share_keeps_own_history_scope(self):
        token=self.csrf()
        with self.client.session_transaction() as session:
            session.update(user_role='doctor',user_id=5)
        with patch('app.patient_records',return_value=({},[])) as authorization:
            response=self.client.post('/qr/share',data={'csrf_token':token,'scope':'history','patient_id':'7','doctor_id':'99'})
            authorization.assert_called_once_with(7)
        with app.test_request_context():
            data=decode_share(response.get_json()['url'].split('#')[1])
            self.assertEqual(data['doctor_id'],5)
        conn=MagicMock();cursor=conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=('Пацієнт','patient@example.com','+38099')
        cursor.fetchall.return_value=[]
        with patch('app.get_db',return_value=conn):
            response=self.client.post('/shared',data={'csrf_token':token,'token':response.get_json()['url'].split('#')[1]})
        self.assertEqual(response.status_code,200)
        self.assertEqual(cursor.execute.call_args.args[1],[7,5])

    def test_invalid_payload_and_permission_rejected(self):
        csrf=self.csrf()
        for payload in [{'v':1,'scope':'all','patient_id':1}, {'v':1,'scope':'profile','patient_id':True}]:
            with app.test_request_context():
                token=share_serializer().dumps(payload)
            self.assertEqual(self.client.post('/shared',data={'csrf_token':csrf,'token':token}).status_code,403)
        with self.client.session_transaction() as session:
            session.update(user_role='doctor',user_id=5)
        with patch('app.patient_records',side_effect=__import__('werkzeug').exceptions.Forbidden):
            self.assertEqual(self.client.post('/qr/share',data={'csrf_token':csrf,'scope':'history','patient_id':'7'}).status_code,403)
