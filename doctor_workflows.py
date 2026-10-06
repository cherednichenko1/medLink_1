"""Routes for the clinician's own calendar and appointment lifecycle."""
from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from db import get_db
from security import now_local, require_role

bp = Blueprint('doctor', __name__, url_prefix='/doctor')
WEEKDAYS = ['Понеділок', 'Вівторок', 'Середа', 'Четвер', 'П’ятниця', 'Субота', 'Неділя']


@bp.route('/appointments')
def appointments():
    require_role('doctor')
    date_value = request.args.get('date', now_local().date().isoformat())
    try:
        selected_date = datetime.strptime(date_value, '%Y-%m-%d').date()
    except ValueError:
        abort(400, description='Некоректна дата прийомів.')
    with get_db().cursor() as cursor:
        cursor.execute('''SELECT a.id, a.appointment_date, a.appointment_time, a.status, u.name, a.user_id
            FROM appointments a LEFT JOIN "user" u ON a.user_id = u.id
            WHERE a.doctor_id = %s AND a.appointment_date = %s ORDER BY a.appointment_time''',
            (session['user_id'], selected_date))
        visits = cursor.fetchall()
    return render_template('doctor_appointments.html', visits=visits, selected_date=selected_date)


@bp.route('/schedule', methods=['GET', 'POST'])
def schedule():
    require_role('doctor')
    conn = get_db()
    with conn.cursor() as cursor:
        if request.method == 'POST':
            hours = []
            for day in range(7):
                if request.form.get(f'enabled_{day}') != 'on':
                    continue
                try:
                    start = datetime.strptime(request.form.get(f'start_{day}', ''), '%H:%M').time()
                    end = datetime.strptime(request.form.get(f'end_{day}', ''), '%H:%M').time()
                    if start >= end or start.minute not in (0,30) or end.minute not in (0,30):
                        raise ValueError
                except ValueError:
                    abort(400, description='Робочий час має бути коректним, початок до завершення; інтервал 30 хвилин.')
                hours.append((session['user_id'], day, start, end))
            # Lock schedule changes against concurrent bookings for this doctor.
            cursor.execute('SELECT id FROM doctor WHERE id = %s FOR UPDATE', (session['user_id'],))
            cursor.execute('DELETE FROM doctor_working_hours WHERE doctor_id = %s', (session['user_id'],))
            for row in hours:
                cursor.execute('INSERT INTO doctor_working_hours(doctor_id,weekday,starts_at,ends_at) VALUES(%s,%s,%s,%s)',row)
            conn.commit()
            flash('Робочий графік збережено. Наявні записи залишаються чинними.', 'success')
            return redirect(url_for('doctor.schedule'), code=303)
        cursor.execute('SELECT weekday, starts_at, ends_at FROM doctor_working_hours WHERE doctor_id = %s', (session['user_id'],))
        hours = {day:(start,end) for day,start,end in cursor.fetchall()}
    return render_template('doctor_schedule.html', weekdays=WEEKDAYS, hours=hours)


@bp.route('/appointments/<int:appointment_id>', methods=['GET', 'POST'])
def visit(appointment_id):
    require_role('doctor')
    conn = get_db()
    with conn.cursor() as cursor:
        query = '''SELECT a.id, a.user_id, a.appointment_date, a.appointment_time, a.status,
            u.name, u.email, u.phone FROM appointments a LEFT JOIN "user" u ON a.user_id = u.id
            WHERE a.id = %s AND a.doctor_id = %s'''
        cursor.execute(query + (' FOR UPDATE OF a' if request.method == 'POST' else ''), (appointment_id,session['user_id']))
        row = cursor.fetchone()
        if not row:
            abort(404)
        visit_data=dict(zip(('id','patient_id','date','time','status','name','email','phone'),row))
        if request.method == 'POST':
            action=request.form.get('action')
            if action == 'start' and row[4] == 'scheduled':
                if datetime.combine(row[2], row[3], tzinfo=now_local().tzinfo) > now_local():
                    abort(400,description='Прийом можна розпочати лише після настання запланованого часу.')
                cursor.execute("UPDATE appointments SET status = 'in_progress' WHERE id = %s",(appointment_id,))
                flash('Прийом розпочато.', 'success')
            elif action == 'complete' and row[4] == 'in_progress':
                if not row[1]:
                    abort(400,description='Для медичного запису пацієнт повинен мати обліковий запис.')
                diagnosis=request.form.get('diagnosis','').strip()
                recommendations=request.form.get('recommendations','').strip()
                medication=request.form.get('medication','').strip()
                if not diagnosis or not recommendations or max(map(len,(diagnosis,recommendations,medication))) > 5000:
                    abort(400,description='Заповніть діагноз і рекомендації; не більше 5000 символів у полі.')
                cursor.execute('''INSERT INTO patient_history(patient_id,doctor_id,appointment_id,diagnosis,recommendations,medication)
                    VALUES(%s,%s,%s,%s,%s,%s)''',(row[1],session['user_id'],appointment_id,diagnosis,recommendations,medication))
                cursor.execute("UPDATE appointments SET status = 'completed' WHERE id = %s",(appointment_id,))
                flash('Прийом завершено. Діагноз і рекомендації додано до історії пацієнта.', 'success')
            elif action == 'cancel' and row[4] == 'scheduled':
                cursor.execute("UPDATE appointments SET status = 'cancelled' WHERE id = %s",(appointment_id,))
                flash('Прийом скасовано лікарем.', 'success')
            else:
                abort(409,description='Стан прийому змінився або дія недоступна. Оновіть сторінку.')
            conn.commit()
            return redirect(url_for('doctor.visit',appointment_id=appointment_id),code=303)
        cursor.execute('SELECT diagnosis,recommendations,medication FROM patient_history WHERE appointment_id = %s',(appointment_id,))
        result=cursor.fetchone()
    return render_template('doctor_visit.html', visit=visit_data, result=result)
