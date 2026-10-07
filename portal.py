"""Responsive authenticated workspace, patient documents and appointment management."""
import io
import warnings
from datetime import datetime, timedelta
import psycopg2
import qrcode
from qrcode.image.svg import SvgPathImage
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, send_file
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import BadRequest
from db import get_db
from security import require_role, require_patient_access, now_local, parse_slot

bp = Blueprint('portal', __name__)
MAX_FILE = 5 * 1024 * 1024
MAX_STORAGE = 20 * 1024 * 1024


def signed_in():
    return session.get('user_id') and session.get('user_role') in ('user','doctor')


@bp.route('/workspace')
def workspace():
    if not signed_in():
        return redirect(url_for('loginPage', next=request.path))
    return redirect(url_for('doctor_cabinet' if session['user_role']=='doctor' else 'user_cabinet'))


@bp.route('/workspace/qr')
def workspace_qr():
    # A universal QR contains a workspace URL, never an account ID or credentials.
    from app import public_url
    stream=io.BytesIO()
    qrcode.make(public_url('portal.workspace'),image_factory=SvgPathImage,box_size=7,border=4).save(stream)
    stream.seek(0)
    return send_file(stream,mimetype='image/svg+xml',as_attachment=False)


@bp.route('/profile',methods=['GET','POST'])
def profile():
    if not signed_in():
        return redirect(url_for('loginPage',next=request.path))
    conn=get_db()
    role=session['user_role']
    with conn.cursor() as cursor:
        if request.method=='POST':
            name=request.form.get('name','').strip()
            phone=request.form.get('phone','').strip()
            if not 2<=len(name)<=150 or not 5<=len(phone)<=30:
                abort(400,description='Перевірте ім’я і телефон.')
            if role=='doctor':
                cursor.execute('UPDATE doctor SET full_name=%s,phone=%s WHERE id=%s RETURNING id',(name,phone,session['user_id']))
            else:
                cursor.execute('UPDATE "user" SET name=%s,phone=%s WHERE id=%s RETURNING id',(name,phone,session['user_id']))
            if not cursor.fetchone(): abort(404)
            conn.commit()
            flash('Контактні дані оновлено.','success')
            return redirect(url_for('portal.profile'),code=303)
        if role=='doctor': cursor.execute('SELECT full_name,phone,email FROM doctor WHERE id=%s',(session['user_id'],))
        else: cursor.execute('SELECT name,phone,email FROM "user" WHERE id=%s',(session['user_id'],))
        person=cursor.fetchone()
        if not person: abort(404)
    return render_template('profile_edit.html',person=person)


@bp.route('/appointments/<int:appointment_id>/reschedule',methods=['GET','POST'])
def reschedule(appointment_id):
    require_role('user')
    conn=get_db()
    with conn.cursor() as cursor:
        # Doctor then appointment: same lock order as bookings and schedule changes.
        cursor.execute('SELECT doctor_id FROM appointments WHERE id=%s AND user_id=%s',(appointment_id,session['user_id']))
        found=cursor.fetchone()
        if not found: abort(404)
        doctor_id=found[0]
        cursor.execute('SELECT id,full_name FROM doctor WHERE id=%s AND verified FOR UPDATE',(doctor_id,))
        doctor=cursor.fetchone()
        cursor.execute('SELECT appointment_date,appointment_time,status FROM appointments WHERE id=%s AND user_id=%s FOR UPDATE',(appointment_id,session['user_id']))
        visit=cursor.fetchone()
        if not visit: abort(404)
        if visit[2]!='scheduled' or datetime.combine(visit[0],visit[1],tzinfo=now_local().tzinfo)<=now_local():
            abort(409,description='Перенести можна лише майбутній запланований прийом.')
        cursor.execute('SELECT weekday,starts_at,ends_at FROM doctor_working_hours WHERE doctor_id=%s ORDER BY weekday',(doctor_id,))
        hours=cursor.fetchall()
        if request.method=='POST':
            try: slot=datetime.combine(*parse_slot(request.form.get('date',''),request.form.get('time',''),use_default_schedule=False))
            except ValueError as error: abort(400,description=str(error))
            if not any(day==slot.weekday() and start<=slot.time() and (slot+timedelta(minutes=30)).date()==slot.date() and (slot+timedelta(minutes=30)).time()<=end for day,start,end in hours):
                abort(400,description='Оберіть час у робочому графіку лікаря.')
            try:
                cursor.execute('UPDATE appointments SET appointment_date=%s,appointment_time=%s WHERE id=%s',(slot.date(),slot.time(),appointment_id))
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                abort(409,description='Цей час уже зайнятий. Ваш попередній запис збережено.')
            flash('Прийом перенесено.','success')
            return redirect(url_for('user_cabinet'),code=303)
    return render_template('reschedule.html',doctor_name=doctor[1],visit=visit,hours=hours)


def validated_file(file):
    if not file or not file.filename: abort(400,description='Оберіть фото або PDF.')
    content=file.read(MAX_FILE+1)
    if not content or len(content)>MAX_FILE: abort(400,description='Файл має бути непорожнім і не більшим за 5 МБ.')
    if content.startswith(b'%PDF-') and b'%%EOF' in content[-1024:]:
        from pdf_safety import sanitize_pdf
        content=sanitize_pdf(content)
        kind='application/pdf'; extension='.pdf'
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as img:
                    if img.format not in ('JPEG','PNG') or img.width*img.height>20_000_000: raise ValueError
                    img.load()
                    clean=ImageOps.exif_transpose(img).convert('RGB')
                    output=io.BytesIO()
                    clean.save(output,format='JPEG',quality=92)
                    content=output.getvalue()
                    if len(content)>MAX_FILE: abort(400,description='Фото після обробки перевищує 5 МБ.')
                    kind='image/jpeg'
                    extension='.jpg'
        except (UnidentifiedImageError,ValueError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):
            abort(400,description='Дозволені лише коректні JPEG, PNG та PDF.')
    name=secure_filename(file.filename.rsplit('.',1)[0])[:100] or 'medical-document'
    return name+extension,kind,content


@bp.route('/patients/<int:patient_id>/documents',methods=['GET','POST'])
def documents(patient_id):
    if not signed_in(): return redirect(url_for('loginPage',next=request.path))
    conn=get_db()
    with conn.cursor() as cursor:
        require_patient_access(cursor,patient_id)
        cursor.execute('SELECT name FROM "user" WHERE id=%s'+(' FOR UPDATE' if request.method=='POST' else ''),(patient_id,))
        patient=cursor.fetchone()
        if not patient: abort(404)
        if request.method=='POST':
            name,kind,content=validated_file(request.files.get('file'))
            title=request.form.get('title','').strip() or name
            if len(title)>200: abort(400,description='Назва має містити до 200 символів.')
            cursor.execute('SELECT COALESCE(SUM(size_bytes),0),COUNT(*) FROM patient_documents WHERE patient_id=%s AND content IS NOT NULL',(patient_id,))
            used,count=cursor.fetchone()
            if used+len(content)>MAX_STORAGE or count>=20: abort(400,description='Ліміт картки: 20 файлів і 20 МБ. Видаліть зайві власні файли.')
            cursor.execute('''INSERT INTO patient_documents(patient_id,uploader_role,uploader_id,title,filename,mime_type,size_bytes,content,safety_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1)''',(patient_id,session['user_role'],session['user_id'],title,name,kind,len(content),psycopg2.Binary(content)))
            conn.commit()
            flash('Документ додано до картки пацієнта.','success')
            return redirect(url_for('portal.documents',patient_id=patient_id),code=303)
        cursor.execute('''SELECT id,title,filename,mime_type,size_bytes,created_at,uploader_role,uploader_id
            FROM patient_documents WHERE patient_id=%s AND content IS NOT NULL ORDER BY created_at DESC,id DESC''',(patient_id,))
        files=cursor.fetchall()
    return render_template('documents.html',patient_name=patient[0],files=files,patient_id=patient_id)


@bp.route('/documents/<int:document_id>/preview', endpoint='preview')
@bp.route('/documents/<int:document_id>/download')
def download(document_id):
    if not signed_in(): abort(403)
    with get_db().cursor() as cursor:
        cursor.execute('SELECT patient_id,filename,mime_type FROM patient_documents WHERE id=%s AND content IS NOT NULL',(document_id,))
        row=cursor.fetchone()
        if not row: abort(404)
        require_patient_access(cursor,row[0])
        cursor.execute('SELECT content,safety_version FROM patient_documents WHERE id=%s AND content IS NOT NULL FOR UPDATE',(document_id,))
        content=cursor.fetchone()
        if not content: abort(404)
        if content[1] == -1:
            abort(400,description='Документ заблоковано перевіркою безпеки. Завантажте звичайний PDF або фото.')
        if content[1] == 0:
            try:
                name,kind,clean=validated_file(FileStorage(stream=io.BytesIO(bytes(content[0])),filename=row[1]))
            except BadRequest:
                cursor.execute('UPDATE patient_documents SET safety_version=-1 WHERE id=%s',(document_id,))
                get_db().commit()
                abort(400,description='Документ заблоковано перевіркою безпеки. Оригінал збережено для перевірки адміністратором.')
            cursor.execute('UPDATE patient_documents SET filename=%s,mime_type=%s,content=%s,size_bytes=%s,safety_version=1 WHERE id=%s',
                           (name,kind,psycopg2.Binary(clean),len(clean),document_id))
            get_db().commit()
            row=(row[0],name,kind); content=(clean,1)
    if request.endpoint == 'portal.preview':
        if row[2] not in ('image/jpeg','image/png'): abort(400)
        with Image.open(io.BytesIO(bytes(content[0]))) as image:
            image.thumbnail((640,640))
            stream=io.BytesIO()
            image.convert('RGB').save(stream,format='JPEG',quality=80)
        stream.seek(0)
        return send_file(stream,mimetype='image/jpeg',as_attachment=False,max_age=0)
    return send_file(io.BytesIO(bytes(content[0])),mimetype=row[2],download_name=row[1],as_attachment=True,max_age=0)


@bp.route('/documents/<int:document_id>/delete',methods=['POST'])
def delete_document(document_id):
    if not signed_in(): abort(403)
    conn=get_db()
    with conn.cursor() as cursor:
        cursor.execute('SELECT patient_id,uploader_role,uploader_id FROM patient_documents WHERE id=%s AND content IS NOT NULL FOR UPDATE',(document_id,))
        row=cursor.fetchone()
        if not row: abort(404)
        require_patient_access(cursor,row[0])
        if (row[1],row[2])!=(session['user_role'],session['user_id']): abort(403,description='Видаляти можна лише власні завантаження.')
        cursor.execute('UPDATE patient_documents SET content=NULL,deleted_at=CURRENT_TIMESTAMP WHERE id=%s',(document_id,))
        conn.commit()
    flash('Файл видалено.','success')
    return redirect(url_for('portal.documents',patient_id=row[0]),code=303)


@bp.route('/profile/avatar',methods=['POST'])
def upload_avatar():
    if not signed_in(): abort(403)
    file=request.files.get('avatar')
    if not file: abort(400,description='Оберіть фото JPEG або PNG.')
    data=file.read(2*1024*1024+1)
    if not data or len(data)>2*1024*1024: abort(400,description='Фото має бути до 2 МБ.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ('JPEG','PNG') or image.width*image.height>20_000_000: raise ValueError
                image=ImageOps.exif_transpose(image)
                image=ImageOps.fit(image.convert('RGB'),(256,256))
                output=io.BytesIO();image.save(output,format='JPEG',quality=85)
    except (ValueError,OSError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        abort(400,description='Потрібне коректне фото JPEG або PNG.')
    conn=get_db()
    with conn.cursor() as c:
        if session['user_role']=='doctor':c.execute('UPDATE doctor SET avatar=%s WHERE id=%s RETURNING id',(psycopg2.Binary(output.getvalue()),session['user_id']))
        else:c.execute('UPDATE "user" SET avatar=%s WHERE id=%s RETURNING id',(psycopg2.Binary(output.getvalue()),session['user_id']))
        if not c.fetchone():abort(404)
    conn.commit();flash('Аватар оновлено.','success')
    return redirect(url_for('portal.profile'),code=303)


@bp.route('/profile/avatar/delete',methods=['POST'])
def delete_avatar():
    if not signed_in():abort(403)
    conn=get_db()
    with conn.cursor() as c:
        if session['user_role']=='doctor':c.execute('UPDATE doctor SET avatar=NULL WHERE id=%s',(session['user_id'],))
        else:c.execute('UPDATE "user" SET avatar=NULL WHERE id=%s',(session['user_id'],))
    conn.commit();flash('Аватар видалено.','success')
    return redirect(url_for('portal.profile'),code=303)


@bp.route('/avatars/<role>/<int:account_id>')
def avatar(role,account_id):
    if role not in ('user','doctor'):abort(404)
    with get_db().cursor() as c:
        if role=='user':require_patient_access(c,account_id)
        if role=='doctor':c.execute('SELECT avatar FROM doctor WHERE id=%s',(account_id,))
        else:c.execute('SELECT avatar FROM "user" WHERE id=%s',(account_id,))
        row=c.fetchone()
        if not row:abort(404)
    if row[0]:return send_file(io.BytesIO(bytes(row[0])),mimetype='image/jpeg',max_age=0)
    from flask import current_app
    return current_app.send_static_file('doctor_placeholder.png' if role=='doctor' else 'user_placeholder.png')
