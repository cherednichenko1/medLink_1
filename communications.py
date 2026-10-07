"""Private patient/doctor messages and LAN WebRTC signalling (no media storage)."""
import json
import uuid
from datetime import timedelta
from flask import Blueprint,abort,redirect,render_template,request,session,url_for,jsonify,send_file
import io
import qrcode
from qrcode.image.svg import SvgPathImage
from db import get_db
from security import positive_id, bounded_cursor, now_local,require_role

bp=Blueprint('communications',__name__)


def identity():
    role=session.get('user_role'); account=session.get('user_id')
    if role not in ('user','doctor') or not account: abort(403)
    return role,account


def relationship(cursor,patient,doctor):
    cursor.execute("""SELECT EXISTS(SELECT 1 FROM appointments WHERE user_id=%s AND doctor_id=%s
        AND status IN ('scheduled','in_progress','completed') UNION ALL
        SELECT 1 FROM patient_history WHERE patient_id=%s AND doctor_id=%s)""",(patient,doctor,patient,doctor))
    if not cursor.fetchone()[0]: abort(403,description='Для спілкування потрібен запис до цього лікаря або попередній медичний запис.')


def thread(cursor,thread_id,lock=False):
    role,account=identity()
    cursor.execute('SELECT patient_id,doctor_id FROM conversations WHERE id=%s'+(' FOR UPDATE' if lock else ''),(thread_id,))
    row=cursor.fetchone()
    if not row: abort(404)
    if (role=='user' and row[0]!=account) or (role=='doctor' and row[1]!=account): abort(403)
    relationship(cursor,*row)
    return row


def contact_rows(c):
    role,account=identity()
    owner='u.id' if role=='user' else 'd.id'
    c.execute(f'''SELECT u.id,u.name,d.id,d.full_name FROM "user" u CROSS JOIN doctor d
        WHERE {owner}=%s AND (EXISTS(SELECT 1 FROM appointments a WHERE a.user_id=u.id AND a.doctor_id=d.id
            AND a.status IN ('scheduled','in_progress','completed'))
        OR EXISTS(SELECT 1 FROM patient_history h WHERE h.patient_id=u.id AND h.doctor_id=d.id))
        ORDER BY d.full_name,u.name''',(account,))
    return c.fetchall()


@bp.route('/messages/contacts')
def contacts():
    role,account=identity()
    with get_db().cursor() as c: rows=contact_rows(c)
    return jsonify(contacts=[dict(id=r[2] if role=='user' else r[0],name=r[3] if role=='user' else r[1],patient_id=r[0]) for r in rows])


@bp.route('/messages')
def inbox():
    if not session.get('user_id'): return redirect(url_for('loginPage',next='/messages'))
    with get_db().cursor() as c:
        partners=contact_rows(c)
    return render_template('inbox.html',partners=partners)


@bp.route('/messages/start/<int:partner_id>',methods=['POST'])
def start(partner_id):
    role,account=identity()
    patient,doctor=(account,partner_id) if role=='user' else (partner_id,account)
    conn=get_db()
    with conn.cursor() as c:
        relationship(c,patient,doctor)
        c.execute('''INSERT INTO conversations(patient_id,doctor_id) VALUES(%s,%s)
            ON CONFLICT(patient_id,doctor_id) DO UPDATE SET patient_id=EXCLUDED.patient_id RETURNING id''',(patient,doctor))
        thread_id=c.fetchone()[0]
    conn.commit()
    if request.accept_mimetypes.best=='application/json':return jsonify(thread_id=thread_id)
    return redirect(url_for('communications.conversation',thread_id=thread_id),code=303)


@bp.route('/messages/<int:thread_id>',methods=['GET','POST'])
def conversation(thread_id):
    if not session.get('user_id'): return redirect(url_for('loginPage',next=request.path))
    conn=get_db()
    with conn.cursor() as c:
        patient,doctor=thread(c,thread_id,request.method=='POST')
        if request.method=='POST':
            text=request.form.get('text','').strip()
            attachment=request.form.get('attachment_id','')
            if len(text)>4000 or (not text and not attachment): abort(400)
            if attachment:
                attachment=positive_id(attachment)
                c.execute('SELECT id FROM patient_documents WHERE id=%s AND patient_id=%s AND content IS NOT NULL',(attachment,patient))
                if not c.fetchone(): abort(400,description='Документ недоступний у цій картці.')
            else: attachment=None
            role,account=identity()
            c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=%s AND sender_role=%s AND created_at>CURRENT_TIMESTAMP-interval '1 minute'",(thread_id,role))
            if c.fetchone()[0]>=20: abort(429)
            c.execute('INSERT INTO messages(conversation_id,sender_role,body,attachment_id) VALUES(%s,%s,%s,%s)',(thread_id,role,text,attachment))
            conn.commit()
            if request.accept_mimetypes.best=='application/json':return jsonify(ok=True)
            return redirect(url_for('communications.conversation',thread_id=thread_id),code=303)
        c.execute('SELECT name FROM "user" WHERE id=%s',(patient,)); patient_name=c.fetchone()[0]
        c.execute('SELECT full_name FROM doctor WHERE id=%s',(doctor,)); doctor_name=c.fetchone()[0]
        c.execute('SELECT id,title FROM patient_documents WHERE patient_id=%s AND content IS NOT NULL ORDER BY created_at DESC',(patient,)); files=c.fetchall()
    return render_template('conversation.html',thread_id=thread_id,patient_id=patient,patient_name=patient_name,doctor_name=doctor_name,files=files)


@bp.route('/messages/<int:thread_id>/updates')
def updates(thread_id):
    after=bounded_cursor(request.args.get('after','0'))
    with get_db().cursor() as c:
        thread(c,thread_id)
        c.execute('''SELECT m.id,m.sender_role,m.body,m.created_at,m.attachment_id,d.title,d.content IS NOT NULL
            FROM messages m LEFT JOIN patient_documents d ON d.id=m.attachment_id
            WHERE m.conversation_id=%s AND m.id>%s ORDER BY m.id LIMIT 100''',(thread_id,after))
        messages=[dict(id=r[0],role=r[1],text=r[2],date=r[3].isoformat(),attachment=r[4] if r[6] else None,title=r[5]) for r in c.fetchall()]
        c.execute('''SELECT id FROM video_calls WHERE conversation_id=%s AND ended_at IS NULL
            AND expires_at>CURRENT_TIMESTAMP ORDER BY created_at DESC LIMIT 1''',(thread_id,))
        call=c.fetchone()
    return jsonify(messages=messages,call=str(call[0]) if call else None)


@bp.route('/messages/<int:thread_id>/qr')
def thread_qr(thread_id):
    with get_db().cursor() as c: thread(c,thread_id)
    from app import public_url
    stream=io.BytesIO(); qrcode.make(public_url('communications.conversation',thread_id=thread_id),image_factory=SvgPathImage).save(stream); stream.seek(0)
    return send_file(stream,mimetype='image/svg+xml')


@bp.route('/messages/<int:thread_id>/call',methods=['POST'])
def start_call(thread_id):
    conn=get_db()
    with conn.cursor() as c:
        thread(c,thread_id,True)
        c.execute('UPDATE video_calls SET ended_at=CURRENT_TIMESTAMP WHERE conversation_id=%s AND ended_at IS NULL AND expires_at<=CURRENT_TIMESTAMP',(thread_id,))
        c.execute('SELECT id FROM video_calls WHERE conversation_id=%s AND ended_at IS NULL',(thread_id,))
        row=c.fetchone()
        if row: call_id=str(row[0])
        else:
            call_id=str(uuid.uuid4())
            c.execute('INSERT INTO video_calls(id,conversation_id,initiator_role,expires_at) VALUES(%s,%s,%s,%s)',(call_id,thread_id,identity()[0],now_local()+timedelta(hours=1)))
        # Remove expired transport metadata; conversation history remains.
        c.execute("DELETE FROM call_signals WHERE call_id IN (SELECT id FROM video_calls WHERE expires_at<CURRENT_TIMESTAMP-interval '1 day')")
    conn.commit()
    if request.accept_mimetypes.best=='application/json':return jsonify(url=url_for('communications.call',call_id=call_id))
    return redirect(url_for('communications.call',call_id=call_id),code=303)


def authorize_call(c,call_id,lock=False):
    c.execute('SELECT conversation_id,initiator_role,expires_at,ended_at FROM video_calls WHERE id=%s'+(' FOR UPDATE' if lock else ''),(str(call_id),))
    row=c.fetchone()
    if not row: abort(404)
    thread(c,row[0])
    if row[2]<=now_local(): abort(410,description='Час дзвінка завершився. Створіть новий.')
    return row


@bp.route('/calls/<uuid:call_id>')
def call(call_id):
    if not session.get('user_id'): return redirect(url_for('loginPage',next=request.path))
    with get_db().cursor() as c:
        row=authorize_call(c,call_id)
        if row[3]: abort(410)
    return render_template('video_call.html',call_id=str(call_id),thread_id=row[0],initiator=row[1]==identity()[0])


@bp.route('/calls/<uuid:call_id>/signals',methods=['GET','POST'])
def signals(call_id):
    conn=get_db(); role,account=identity()
    with conn.cursor() as c:
        row=authorize_call(c,call_id,request.method=='POST')
        if request.method=='POST':
            if row[3]: abort(410)
            kind=request.form.get('kind')
            if kind not in ('offer','answer','candidate','hangup'): abort(400)
            if kind=='offer' and row[1]!=role or kind=='answer' and row[1]==role: abort(403)
            raw=request.form.get('payload','{}')
            if len(raw)>24000: abort(400)
            try: payload=json.loads(raw)
            except (ValueError,TypeError): abort(400)
            if not isinstance(payload,dict): abort(400)
            if kind in ('offer','answer') and (payload.get('type')!=kind or not isinstance(payload.get('sdp'),str) or len(payload['sdp'])>20000): abort(400)
            if kind=='candidate':
                if not isinstance(payload.get('candidate'),str) or len(payload['candidate'])>2000: abort(400)
                mid=payload.get('sdpMid'); index=payload.get('sdpMLineIndex')
                if mid is not None and (not isinstance(mid,str) or len(mid)>128): abort(400)
                if index is not None and (type(index) is not int or not 0<=index<=65535): abort(400)
            if kind in ('offer','answer'):
                c.execute('SELECT EXISTS(SELECT 1 FROM call_signals WHERE call_id=%s AND kind=%s)',(str(call_id),kind))
                if c.fetchone()[0]: abort(409)
                if kind=='answer':
                    c.execute("SELECT EXISTS(SELECT 1 FROM call_signals WHERE call_id=%s AND kind='offer')",(str(call_id),))
                    if not c.fetchone()[0]: abort(409)
            c.execute('SELECT COUNT(*) FROM call_signals WHERE call_id=%s AND sender_role=%s',(str(call_id),role))
            if c.fetchone()[0]>=300 and kind!='hangup': abort(429)
            c.execute('INSERT INTO call_signals(call_id,sender_role,kind,payload) VALUES(%s,%s,%s,%s)',(str(call_id),role,kind,json.dumps(payload)))
            if kind=='hangup': c.execute('UPDATE video_calls SET ended_at=CURRENT_TIMESTAMP WHERE id=%s',(str(call_id),))
            conn.commit()
            return jsonify(ok=True)
        after=bounded_cursor(request.args.get('after','0'))
        c.execute('SELECT id,kind,payload FROM call_signals WHERE call_id=%s AND sender_role<>%s AND id>%s ORDER BY id LIMIT 100',(str(call_id),role,after))
        result=[dict(id=r[0],kind=r[1],payload=json.loads(r[2])) for r in c.fetchall()]
    return jsonify(signals=result,ended=bool(row[3]))
