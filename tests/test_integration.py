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
from security import now_local, issue_session

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
            cursor.execute('TRUNCATE call_signals, video_calls, messages, conversations, patient_documents, appointments, patient_history, doctor, "user", login_attempts, action_limits RESTART IDENTITY CASCADE')
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
            cursor.execute("UPDATE doctor SET verified=TRUE")
            cursor.execute("INSERT INTO doctor_working_hours(doctor_id,weekday,starts_at,ends_at) SELECT id,day,'09:00'::time,'17:00'::time FROM doctor CROSS JOIN generate_series(0,4) day")
        self.client = app.test_client()
        day = now_local().date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        self.day = day.isoformat()

    def test_logout_revokes_copied_session_and_issued_qr(self):
        token=self.authenticate()
        response=self.client.post('/qr/share',data={'csrf_token':token,'scope':'profile','patient_id':'1'})
        target=response.get_json()['url']
        from urllib.parse import urlsplit
        target=urlsplit(target).path+'?'+urlsplit(target).query
        guest=app.test_client()
        self.assertEqual(guest.get(target).status_code,200)
        cookie=self.client.get_cookie(app.config['SESSION_COOKIE_NAME']).value
        self.assertEqual(self.client.post('/logout',data={'csrf_token':token}).status_code,303)
        stolen=app.test_client(); stolen.set_cookie(app.config['SESSION_COOKIE_NAME'],cookie)
        self.assertEqual(stolen.get('/user_info/1').status_code,302)
        self.assertEqual(guest.get(target).status_code,403)

    def test_manual_qr_revocation_keeps_login(self):
        token=self.authenticate()
        target=self.client.post('/qr/share',data={'csrf_token':token,'scope':'profile','patient_id':'1'}).get_json()['url']
        from urllib.parse import urlsplit
        path=urlsplit(target).path+'?'+urlsplit(target).query
        self.assertEqual(app.test_client().get(path).status_code,200)
        self.assertEqual(self.client.post('/qr/shares/revoke',data={'csrf_token':token}).status_code,303)
        self.assertEqual(app.test_client().get(path).status_code,403)
        self.assertEqual(self.client.get('/user_info/1').status_code,200)

    def test_sessions_have_absolute_expiry_and_account_binding(self):
        self.authenticate()
        with closing(db.get_db_connection()) as conn,conn,conn.cursor() as cursor:
            cursor.execute("UPDATE auth_sessions SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second'")
        self.assertEqual(self.client.get('/user_info/1').status_code,302)
        self.authenticate()
        with self.client.session_transaction() as state: state['user_id']=2
        self.assertEqual(self.client.get('/user_info/2').status_code,302)

    def test_legacy_cookie_cannot_grant_access(self):
        with self.client.session_transaction() as state: state.update(user_role='doctor',user_id=1)
        self.assertEqual(self.client.get('/doctorCabinet').status_code,302)

    def test_doctor_revocation_blocks_live_session_and_shares(self):
        self.book()
        token=self.authenticate(role='doctor')
        target=self.client.post('/qr/share',data={'csrf_token':token,'scope':'history','patient_id':'1'}).get_json()['url']
        result=app.test_cli_runner().invoke(args=['verify-doctor','--doctor-id','1','--revoke'])
        self.assertEqual(result.exit_code,0,result.output)
        self.assertEqual(self.client.get('/doctorCabinet').status_code,302)
        from urllib.parse import urlsplit
        self.assertEqual(app.test_client().get(urlsplit(target).path+'?'+urlsplit(target).query).status_code,403)

    def test_unverified_doctor_cannot_receive_bookings(self):
        with closing(db.get_db_connection()) as conn,conn,conn.cursor() as cursor:
            cursor.execute('UPDATE doctor SET verified=FALSE WHERE id=1')
        token=self.authenticate()
        response=self.client.post('/searchPage',data={'csrf_token':token,'doctor_id':'1','appointment_date':self.day,'appointment_time':'09:00'})
        self.assertEqual(response.status_code,404)
        self.assertNotIn(b'/doctor_info/doctor@example.com',self.client.get('/searchPage').data)

    def test_large_ids_and_pagination_return_400(self):
        self.authenticate()
        self.assertEqual(self.client.get('/user_info/999999999999999999999999').status_code,400)
        self.assertEqual(self.client.get('/messages/1/updates?after=99999999999999999999999').status_code,400)
        self.assertEqual(self.client.get('/messages/1/updates?after=-1').status_code,400)

    def test_expensive_uploads_are_rate_limited_per_account(self):
        token=self.authenticate()
        for _ in range(10):
            self.assertEqual(self.client.post('/profile/avatar',data={'csrf_token':token}).status_code,400)
        self.assertEqual(self.client.post('/profile/avatar',data={'csrf_token':token}).status_code,429)

    def test_legacy_unsafe_pdf_is_quarantined_without_deletion(self):
        token=self.authenticate()
        content=b'%PDF-1.7 fake %%EOF'
        with closing(db.get_db_connection()) as conn,conn,conn.cursor() as cursor:
            cursor.execute("INSERT INTO patient_documents(patient_id,uploader_role,uploader_id,title,filename,mime_type,size_bytes,content) VALUES(1,'user',1,'Legacy','legacy.pdf','application/pdf',%s,%s) RETURNING id",(len(content),db.psycopg2.Binary(content)))
            document=cursor.fetchone()[0]
        self.assertEqual(self.client.get(f'/documents/{document}/download').status_code,400)
        with closing(db.get_db_connection()) as conn,conn.cursor() as cursor:
            cursor.execute('SELECT safety_version,content FROM patient_documents WHERE id=%s',(document,)); row=cursor.fetchone()
            self.assertEqual(row[0],-1); self.assertEqual(bytes(row[1]),content)
        self.assertEqual(self.client.get(f'/documents/{document}/download').status_code,400)

    def test_qr_active_grants_are_bounded(self):
        token=self.authenticate()
        for _ in range(20):
            self.assertEqual(self.client.post('/qr/share',data={'csrf_token':token,'scope':'profile','patient_id':'1'}).status_code,200)
        self.assertEqual(self.client.post('/qr/share',data={'csrf_token':token,'scope':'profile','patient_id':'1'}).status_code,429)

    def csrf(self, client=None):
        client = client or self.client
        client.get('/loginPage')
        with client.session_transaction() as session:
            return session['_csrf']

    def authenticate(self, role='user', account_id=1, client=None):
        client = client or self.client
        token = self.csrf(client)
        with client.session_transaction() as session:
            with closing(db.get_db_connection()) as conn, conn, conn.cursor() as cursor:
                sid=issue_session(cursor, role, account_id)
            session.update(user_role=role, user_id=account_id, _sid=sid)
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

    def test_avatar_owner_upload_and_patient_privacy(self):
        import io
        from PIL import Image
        image=io.BytesIO();Image.new('RGB',(80,120),'red').save(image,format='PNG')
        token=self.authenticate()
        response=self.client.post('/profile/avatar',data={'csrf_token':token,'avatar':(io.BytesIO(image.getvalue()),'photo.png'),'user_id':'2'})
        self.assertEqual(response.status_code,303)
        avatar=self.client.get('/avatars/user/1')
        self.assertEqual(avatar.mimetype,'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(avatar.data)).size,(256,256))
        with closing(db.get_db_connection()) as conn,conn.cursor() as c:
            c.execute('SELECT avatar FROM "user" WHERE id=2');self.assertIsNone(c.fetchone()[0])
        self.authenticate(account_id=2)
        self.assertEqual(self.client.get('/avatars/user/1').status_code,403)
        token=self.authenticate()
        self.assertEqual(self.client.post('/profile/avatar/delete',data={'csrf_token':token}).status_code,303)
        with closing(db.get_db_connection()) as conn,conn.cursor() as c:
            c.execute('SELECT avatar FROM "user" WHERE id=1');self.assertIsNone(c.fetchone()[0])

    def test_avatar_invalid_type_and_csrf(self):
        import io
        token=self.authenticate(role='doctor')
        self.assertEqual(self.client.post('/profile/avatar',data={'csrf_token':token,'avatar':(io.BytesIO(b'<script>bad</script>'),'x.png')}).status_code,400)
        self.assertEqual(self.client.post('/profile/avatar',data={'avatar':(io.BytesIO(b'fake'),'x.png')}).status_code,400)

    def test_mini_chat_json_matches_full_conversation(self):
        self.book();token=self.authenticate()
        headers={'Accept':'application/json'}
        contacts=self.client.get('/messages/contacts').json['contacts']
        self.assertEqual([p['id'] for p in contacts],[1])
        response=self.client.post('/messages/start/1',data={'csrf_token':token},headers=headers)
        thread=response.json['thread_id'];route=f'/messages/{thread}'
        self.assertEqual(self.client.post(route,data={'csrf_token':token,'text':'Міні-чат'},headers=headers).json,{'ok':True})
        self.assertEqual(self.client.get(route+'/updates').json['messages'][0]['text'],'Міні-чат')
        self.assertTrue(self.client.post(route+'/call',data={'csrf_token':token},headers=headers).json['url'].startswith('/calls/'))
        self.authenticate(role='doctor',account_id=2)
        self.assertEqual(self.client.get('/messages/contacts').json['contacts'],[])
        self.assertEqual(self.client.get(route+'/updates').status_code,403)

    def open_chat(self):
        self.book()
        token=self.authenticate()
        response=self.client.post('/messages/start/1',data={'csrf_token':token})
        self.assertEqual(response.status_code,303)
        return response.location,token

    def test_chat_messages_and_membership(self):
        route,token=self.open_chat()
        self.assertEqual(self.client.post(route,data={'csrf_token':token,'text':'Фото додано'}).status_code,303)
        self.assertEqual(self.client.get(route+'/updates').json['messages'][0]['text'],'Фото додано')
        self.authenticate(role='doctor')
        self.assertEqual(self.client.get(route).status_code,200)
        self.assertEqual(self.client.get(route+'/qr').mimetype,'image/svg+xml')
        self.authenticate(role='doctor',account_id=2)
        self.assertEqual(self.client.get(route+'/updates').status_code,403)
        self.authenticate(account_id=2)
        self.assertEqual(self.client.get(route).status_code,403)

    def test_chat_rejects_unrelated_partner_and_csrf(self):
        token=self.authenticate()
        self.assertEqual(self.client.post('/messages/start/2',data={'csrf_token':token}).status_code,403)
        self.assertEqual(self.client.post('/messages/start/1',data={}).status_code,400)

    def test_chat_qr_login_preserves_target(self):
        route,token=self.open_chat()
        self.client.post('/logout',data={'csrf_token':token})
        self.assertIn('/loginPage?next=',self.client.get(route).location)
        token=self.csrf()
        response=self.client.post('/getLogin',data={'csrf_token':token,'role':'user','enterLogin':'patient@example.com','enterPassword':self.password,'next':route})
        self.assertEqual(response.location,route)

    def test_call_signals_private_roles_and_hangup(self):
        import json
        route,token=self.open_chat()
        response=self.client.post(route+'/call',data={'csrf_token':token})
        call=response.location
        self.assertEqual(self.client.post(route+'/call',data={'csrf_token':token}).location,call)
        signal=call+'/signals'
        self.assertEqual(self.client.post(signal,data={'csrf_token':token,'kind':'offer','payload':json.dumps({'type':'offer','sdp':'v=0'})}).status_code,200)
        token=self.authenticate(role='doctor')
        self.assertEqual(self.client.get(signal).json['signals'][0]['kind'],'offer')
        self.assertEqual(self.client.post(signal,data={'csrf_token':token,'kind':'offer','payload':json.dumps({'type':'offer','sdp':'v=0'})}).status_code,403)
        self.assertEqual(self.client.post(signal,data={'csrf_token':token,'kind':'answer','payload':json.dumps({'type':'answer','sdp':'v=0'})}).status_code,200)
        self.authenticate(role='doctor',account_id=2)
        self.assertEqual(self.client.get(signal).status_code,403)
        token=self.authenticate()
        self.assertEqual(self.client.post(signal,data={'csrf_token':token,'kind':'hangup','payload':'{}'}).status_code,200)
        self.assertTrue(self.client.get(signal).json['ended'])
        self.assertEqual(self.client.get(call).status_code,410)
        self.assertNotEqual(self.client.post(route+'/call',data={'csrf_token':token}).location,call)

    def test_call_rejects_bad_signals_and_expiry(self):
        route,token=self.open_chat()
        call=self.client.post(route+'/call',data={'csrf_token':token}).location
        self.assertEqual(self.client.post(call+'/signals',data={'csrf_token':token,'kind':'candidate','payload':'[]'}).status_code,400)
        self.assertEqual(self.client.post(call+'/signals',data={'kind':'hangup'}).status_code,400)
        with closing(db.get_db_connection()) as conn,conn,conn.cursor() as cursor:
            cursor.execute("UPDATE video_calls SET expires_at=CURRENT_TIMESTAMP-interval '1 minute'")
        self.assertEqual(self.client.get(call).status_code,410)

    def test_workspace_qr_and_roles(self):
        self.assertEqual(self.client.get('/workspace').status_code,302)
        self.assertEqual(self.client.get('/workspace/qr').mimetype,'image/svg+xml')
        self.authenticate()
        self.assertTrue(self.client.get('/workspace').location.endswith('/userCabinet'))
        self.authenticate(role='doctor')
        self.assertTrue(self.client.get('/workspace').location.endswith('/doctorCabinet'))

    def test_profile_updates_only_current_account(self):
        token=self.authenticate()
        response=self.client.post('/profile',data={'csrf_token':token,'name':'Нове ім’я','phone':'+380991234500','user_id':'2'})
        self.assertEqual(response.status_code,303)
        with closing(db.get_db_connection()) as conn,conn.cursor() as cursor:
            cursor.execute('SELECT name FROM "user" ORDER BY id')
            self.assertEqual(cursor.fetchall(),[('Нове ім’я',),('Інший',)])

    def test_reschedule_rollback_and_ownership(self):
        self.book()
        with closing(db.get_db_connection()) as conn,conn,conn.cursor() as cursor:
            cursor.execute('SELECT id FROM appointments'); appointment=cursor.fetchone()[0]
            cursor.execute("INSERT INTO appointments(doctor_id,user_id,appointment_date,appointment_time) VALUES(1,2,%s,'10:00')",(self.day,))
        token=self.authenticate()
        route=f'/appointments/{appointment}/reschedule'
        self.assertEqual(self.client.post(route,data={'csrf_token':token,'date':self.day,'time':'10:00'}).status_code,409)
        with closing(db.get_db_connection()) as conn,conn.cursor() as cursor:
            cursor.execute('SELECT appointment_time FROM appointments WHERE id=%s',(appointment,))
            self.assertEqual(str(cursor.fetchone()[0]),'09:00:00')
        self.assertEqual(self.client.post(route,data={'csrf_token':token,'date':self.day,'time':'11:00'}).status_code,303)
        self.authenticate(account_id=2)
        self.assertEqual(self.client.get(route).status_code,404)

    def test_documents_upload_download_permissions_and_deletion(self):
        import io
        from PIL import Image
        image=io.BytesIO(); Image.frombytes('RGB',(256,256),os.urandom(256*256*3)).save(image,format='PNG'); content=image.getvalue()
        self.book()
        token=self.authenticate()
        route='/patients/1/documents'
        self.assertEqual(self.client.post(route,data={'csrf_token':token,'title':'Аналізи','file':(io.BytesIO(content),'photo.png')}).status_code,303)
        with closing(db.get_db_connection()) as conn,conn.cursor() as cursor:
            cursor.execute('SELECT id FROM patient_documents'); document=cursor.fetchone()[0]
        download=f'/documents/{document}/download'
        response=self.client.get(download)
        self.assertTrue(response.data.startswith(b'\xff\xd8'))
        self.assertEqual(response.mimetype,'image/jpeg')
        self.assertNotEqual(response.data,content)
        self.assertIn('attachment',response.headers['Content-Disposition'])
        self.assertGreater(len(content),64*1024)
        preview=self.client.get(f'/documents/{document}/preview')
        self.assertEqual(preview.mimetype,'image/jpeg')
        self.assertEqual(preview.headers['Cache-Control'],'no-store')
        self.authenticate(role='doctor')
        self.assertEqual(self.client.get(download).status_code,200)
        self.authenticate(role='doctor',account_id=2)
        self.assertEqual(self.client.get(download).status_code,403)
        self.authenticate(account_id=2)
        self.assertEqual(self.client.get(download).status_code,403)
        token=self.authenticate()
        self.assertEqual(self.client.post(f'/documents/{document}/delete',data={'csrf_token':token}).status_code,303)
        self.assertEqual(self.client.get(download).status_code,404)
        with closing(db.get_db_connection()) as conn,conn.cursor() as cursor:
            cursor.execute('SELECT content,deleted_at FROM patient_documents WHERE id=%s',(document,))
            data=cursor.fetchone(); self.assertIsNone(data[0]); self.assertIsNotNone(data[1])

    def test_document_rejects_spoofed_type_and_guest(self):
        import io
        token=self.csrf()
        self.assertEqual(self.client.post('/patients/1/documents',data={'csrf_token':token,'file':(io.BytesIO(b'fake'),'x.png')}).status_code,302)
        token=self.authenticate()
        self.assertEqual(self.client.post('/patients/1/documents',data={'csrf_token':token,'file':(io.BytesIO(b'<script>alert(1)</script>'),'x.png')}).status_code,400)
        self.assertEqual(self.client.post('/patients/1/documents',data={'file':(io.BytesIO(b'fake'),'x.png')}).status_code,400)

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
            self.assertNotIn('user_role',session)
        result=app.test_cli_runner().invoke(args=['verify-doctor','--doctor-id',str(doctor_id)])
        self.assertEqual(result.exit_code,0,result.output)
        response=self.client.post('/getLogin',data={'csrf_token':token,'role':'doctor','enterLogin':'newdoctor@example.com','enterPassword':self.password})
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
