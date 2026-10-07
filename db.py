"""Request-scoped connections and serialized, transactional database upgrades."""
import os
import time
from contextlib import closing

import psycopg2
from flask import g
from werkzeug.security import generate_password_hash

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': os.environ.get('DB_PORT', '5432'),
    'dbname': os.environ.get('DB_NAME', 'medlink'),
    'user': os.environ.get('DB_USER', 'medlink'),
    'password': os.environ.get('DB_PASSWORD', 'medlink'),
    'connect_timeout': 5,
    'options': '-c statement_timeout=15000',
}


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def get_db():
    if 'db' not in g:
        g.db = get_db_connection()
    return g.db


def close_db(error=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()  # rolls back unfinished transactions, including on errors


def wait_for_db(max_retries=30, delay=2):
    for attempt in range(max_retries):
        try:
            with closing(get_db_connection()):
                return
        except psycopg2.OperationalError:
            if attempt == max_retries - 1:
                raise RuntimeError('PostgreSQL недоступний після повторних спроб.') from None
            time.sleep(delay)


def init_db():
    """Run explicitly before Gunicorn. Concurrent pods serialize via a transaction lock."""
    wait_for_db()
    with closing(get_db_connection()) as conn, conn, conn.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '0'")
        cursor.execute("SET LOCAL lock_timeout = '120s'")
        cursor.execute('SELECT pg_advisory_xact_lock(123456789)')
        cursor.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)''')
        cursor.execute('SELECT version FROM schema_migrations WHERE version = 1')
        if cursor.fetchone():
            migrate_doctor_workflows(cursor)
            migrate_portal(cursor)
            return
        cursor.execute('''CREATE TABLE IF NOT EXISTS district (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS hospital (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
            district_id INTEGER NOT NULL REFERENCES district(id), edrpou TEXT NOT NULL, address TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS specialization (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS doctor (
            id SERIAL PRIMARY KEY, full_name TEXT NOT NULL, phone TEXT NOT NULL,
            experience INTEGER NOT NULL, hospital_id INTEGER NOT NULL REFERENCES hospital(id),
            specialization_id INTEGER NOT NULL REFERENCES specialization(id),
            rnokpp TEXT UNIQUE, email TEXT UNIQUE, password TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS "user" (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE, phone TEXT, password TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS patient_history (
            id SERIAL PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES "user"(id),
            doctor_id INTEGER NOT NULL REFERENCES doctor(id), recommendations TEXT,
            medication TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        cursor.execute('''SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'patient_history' ''')
        columns = {row[0] for row in cursor.fetchall()}
        if 'diagnosis' in columns and 'recommendations' not in columns:
            cursor.execute('ALTER TABLE patient_history RENAME COLUMN diagnosis TO recommendations')
        cursor.execute('''CREATE TABLE IF NOT EXISTS appointments (
            id SERIAL PRIMARY KEY, doctor_id INTEGER NOT NULL REFERENCES doctor(id),
            user_id INTEGER REFERENCES "user"(id), email TEXT,
            appointment_date DATE NOT NULL, appointment_time TIME NOT NULL)''')
        # Casting validates legacy data. On invalid or duplicate slots the whole upgrade
        # rolls back, retaining all records for explicit operator resolution.
        cursor.execute('''ALTER TABLE appointments
            ALTER COLUMN appointment_date TYPE DATE USING appointment_date::date,
            ALTER COLUMN appointment_time TYPE TIME USING appointment_time::time''')
        cursor.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'scheduled'")
        cursor.execute('''ALTER TABLE appointments ADD CONSTRAINT appointments_status_check
            CHECK (status IN ('scheduled', 'cancelled'))''')
        cursor.execute('''CREATE UNIQUE INDEX IF NOT EXISTS appointments_slot_unique
            ON appointments (doctor_id, appointment_date, appointment_time) WHERE status = 'scheduled' ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS appointments_user_idx ON appointments(user_id, appointment_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS history_patient_doctor_idx ON patient_history(patient_id, doctor_id, timestamp DESC)')
        # All old passwords in this project were plaintext. A migration marker,
        # rather than a prefix guess, prevents plaintext passwords resembling hashes
        # from being accidentally treated as already hashed.
        for table, index in (('"user"', 'users_email_ci_unique'), ('doctor', 'doctors_email_ci_unique')):
            cursor.execute(f'SELECT id, password FROM {table} WHERE password IS NOT NULL')
            for account_id, password in cursor.fetchall():
                cursor.execute(f'UPDATE {table} SET password = %s WHERE id = %s',
                               (generate_password_hash(password, method='pbkdf2:sha256:1000000'), account_id))
            cursor.execute(f'UPDATE {table} SET email = lower(trim(email)) WHERE email IS NOT NULL')
            cursor.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} (lower(email))')
        cursor.execute('''CREATE TABLE IF NOT EXISTS login_attempts (
            key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
            window_start TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        cursor.execute('SELECT COUNT(*) FROM hospital')
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO district (name) VALUES ('Тестовий район') RETURNING id")
            district_id = cursor.fetchone()[0]
            cursor.execute('''INSERT INTO hospital (name, type, district_id, edrpou, address)
                VALUES ('Тестова лікарня', 'Державна', %s, '12345678', 'вул. Тестова, 1')''', (district_id,))
        cursor.execute('SELECT COUNT(*) FROM specialization')
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO specialization (name) VALUES ('Терапевт')")
        cursor.execute('INSERT INTO schema_migrations (version) VALUES (1)')
        migrate_doctor_workflows(cursor)
        migrate_portal(cursor)
    print('MedLink: міграцію БД завершено.')


def migrate_doctor_workflows(cursor):
    cursor.execute('SELECT version FROM schema_migrations WHERE version = 2')
    if cursor.fetchone():
        return
    cursor.execute('ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_status_check')
    cursor.execute("""ALTER TABLE appointments ADD CONSTRAINT appointments_status_check
        CHECK (status IN ('scheduled', 'in_progress', 'completed', 'cancelled'))""")
    cursor.execute('DROP INDEX IF EXISTS appointments_slot_unique')
    cursor.execute("""CREATE UNIQUE INDEX appointments_slot_unique ON appointments
        (doctor_id, appointment_date, appointment_time) WHERE status IN ('scheduled','in_progress')""")
    cursor.execute('ALTER TABLE patient_history ADD COLUMN IF NOT EXISTS diagnosis TEXT')
    cursor.execute('ALTER TABLE patient_history ADD COLUMN IF NOT EXISTS appointment_id INTEGER REFERENCES appointments(id)')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS history_appointment_unique ON patient_history(appointment_id) WHERE appointment_id IS NOT NULL')
    cursor.execute("""CREATE TABLE IF NOT EXISTS doctor_working_hours (
        doctor_id INTEGER NOT NULL REFERENCES doctor(id), weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
        starts_at TIME NOT NULL, ends_at TIME NOT NULL,
        CHECK (starts_at < ends_at), PRIMARY KEY (doctor_id, weekday))""")
    cursor.execute("""INSERT INTO doctor_working_hours (doctor_id, weekday, starts_at, ends_at)
        SELECT d.id, day, '09:00'::time, '17:00'::time FROM doctor d CROSS JOIN generate_series(0,4) day
        ON CONFLICT DO NOTHING""")
    cursor.execute('INSERT INTO schema_migrations(version) VALUES (2)')


if __name__ == '__main__':
    init_db()


def migrate_portal(cursor):
    cursor.execute('SELECT version FROM schema_migrations WHERE version = 3')
    if cursor.fetchone(): return
    cursor.execute("""CREATE TABLE patient_documents (
        id SERIAL PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES "user"(id),
        uploader_role TEXT NOT NULL CHECK(uploader_role IN ('user','doctor')), uploader_id INTEGER NOT NULL,
        title TEXT NOT NULL, filename TEXT NOT NULL, mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes > 0 AND size_bytes <= 5242880),
        content BYTEA, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TIMESTAMPTZ,
        CHECK((content IS NULL) = (deleted_at IS NOT NULL)))""")
    cursor.execute('CREATE INDEX documents_patient_idx ON patient_documents(patient_id,created_at DESC) WHERE content IS NOT NULL')
    cursor.execute('INSERT INTO schema_migrations(version) VALUES(3)')
