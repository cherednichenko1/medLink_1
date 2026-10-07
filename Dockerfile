FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 medlink \
    && useradd --uid 10001 --gid medlink --no-create-home medlink

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py security.py doctor_workflows.py portal.py ./
COPY templates/ templates/
COPY static/ static/

USER medlink
EXPOSE 5000

# Schema initialization runs once per container, before worker processes start.
# The transaction lock serializes concurrent Kubernetes replicas.
CMD ["sh", "-c", "python -c 'from app import init_db; init_db()' && exec gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 60 --access-logfile - --access-logformat '%(h)s %(s)s %(L)s' --error-logfile - app:app"]
