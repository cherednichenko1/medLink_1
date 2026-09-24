# Розгортання MedLink у Kubernetes (minikube) на macOS

Стек: Flask (gunicorn) + PostgreSQL + nginx (reverse proxy), усе в кластері minikube.

## 0. Що встановити

```bash
brew install --cask docker          # Docker Desktop (потрібен для збірки образів)
brew install minikube kubectl
```

Відкрий Docker Desktop і дочекайся, поки він повністю запуститься (іконка кита в меню-барі стане активною).

Перевір встановлення:
```bash
docker --version
minikube version
kubectl version --client
```

## 1. Запусти локальний кластер

```bash
minikube start --cpus=4 --memory=6g --driver=docker
```

Якщо мало ресурсів на ноуті — зменш `--cpus`/`--memory` (мінімум 2 CPU / 4g для трьох сервісів).

Перевір, що кластер живий:
```bash
kubectl get nodes
```

## 2. Збери Docker-образ застосунку ВСЕРЕДИНІ minikube

Це ключовий момент: minikube має свій власний Docker-демон, окремий від того, що на ноуті. Якщо зібрати образ звичайним `docker build`, кластер його не побачить. Тому:

```bash
cd medlink          # тека з Dockerfile, app.py, k8s/ і т.д.
eval $(minikube docker-env)
docker build -t medlink-app:latest .
```

Перевір, що образ з'явився саме в demon-і minikube:
```bash
docker images | grep medlink-app
```

⚠️ Команда `eval $(minikube docker-env)` діє тільки в поточному терміналі. Якщо відкриєш новий термінал — треба повторити.

## 3. Застосуй k8s-маніфести

Файли пронумеровані — накатуй по порядку (або просто `kubectl apply -f k8s/`, k8s сам розбереться з namespace завдяки номерам):

```bash
kubectl apply -f k8s/
```

Перевір, що все піднялось:
```bash
kubectl get all -n medlink
kubectl get pods -n medlink -w    # почекай, поки всі стануть Running (Ctrl+C щоб вийти)
```

Якщо под flask-app в CrashLoopBackOff — подивись логи:
```bash
kubectl logs -n medlink deployment/flask-app
kubectl logs -n medlink deployment/postgres
```

Найчастіша причина — Postgres ще не встиг піднятись до першого запиту Flask. У `app.py` вже є retry-логіка (`wait_for_db`), тож под має сам перезапуститись і зʼєднатись за кілька спроб.

## 4. Відкрий застосунок у браузері

```bash
minikube service nginx-service -n medlink
```

Ця команда сама відкриє браузер з правильним IP:port (в minikube NodePort треба тунелювати через сам minikube, звичайний `localhost:30080` напряму може не спрацювати на macOS з driver=docker).

Альтернатива — тримати окремий термінал відкритим:
```bash
minikube tunnel
```
і тоді звертатись за адресою, яку покаже `kubectl get svc -n medlink`.

## 5. Корисні команди для дебагу

```bash
# Зайти всередину пода Flask
kubectl exec -it -n medlink deployment/flask-app -- /bin/bash

# Зайти в Postgres і подивитись таблиці
kubectl exec -it -n medlink deployment/postgres -- psql -U medlink -d medlink -c '\dt'

# Перезапустити деплоймент після зміни коду (після нової збірки образу)
kubectl rollout restart deployment/flask-app -n medlink

# Подивитись, чому под не стартує
kubectl describe pod -n medlink <ім'я_пода>

# Прибрати все і почати заново
kubectl delete namespace medlink
```

## 6. Якщо міняєш код app.py

Після будь-якої зміни в `app.py`, `templates/` чи `static/` треба перезібрати образ (все ще в тому ж терміналі з `eval $(minikube docker-env)`):

```bash
docker build -t medlink-app:latest .
kubectl rollout restart deployment/flask-app -n medlink
```

## 7. Швидка перевірка без k8s (опційно, але рекомендовано перед деплоєм)

Перш ніж возитись із kubectl, варто переконатись, що застосунок і БД взагалі дружать одне з одним:

```bash
docker compose up --build
```

Відкрий http://localhost:8080 — це той самий стек (Flask + Postgres + nginx), тільки через docker-compose. Якщо тут щось не так — легше дебажити, ніж одразу в кластері.

Зупинити:
```bash
docker compose down        # дані зостануться (volume pgdata)
docker compose down -v     # видалити і дані теж
```

## Структура проєкту

```
medlink/
├── app.py                    # Flask-застосунок (PostgreSQL замість SQLite)
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── docker-compose.yml        # для локальної перевірки без k8s
├── nginx/
│   └── nginx.conf            # конфіг для docker-compose варіанту
├── templates/                 # HTML-шаблони (скопійовані з проєкту)
├── static/                    # CSS/JS/зображення
└── k8s/
    ├── 00-namespace.yaml
    ├── 01-secrets.yaml        # ⚠️ зміни паролі перед реальним використанням
    ├── 02-postgres-pvc.yaml
    ├── 03-postgres-deployment.yaml
    ├── 04-postgres-service.yaml
    ├── 05-flask-configmap.yaml
    ├── 06-flask-deployment.yaml
    ├── 07-flask-service.yaml
    ├── 08-nginx-configmap.yaml
    ├── 09-nginx-deployment.yaml
    └── 10-nginx-service.yaml
```

## Відомі нюанси проєкту (не пов'язані з деплоєм, але варто знати)

- `registerForm.html` у вихідному проєкті не відповідає полям, які читає `/registerPage`
  (форма має `username`/`confirm-password`, а бекенд очікує `role`/`name`/`email`/`phone`/`rnokpp`/...).
  Реєстрація в поточному вигляді працювати не буде — це вже було так у вихідному коді,
  я це не займав, оскільки завдання було саме про деплой.
- Паролі користувачів/лікарів зберігаються у відкритому вигляді в БД — варто буде додати
  хешування (наприклад, `werkzeug.security.generate_password_hash`) окремим кроком.
