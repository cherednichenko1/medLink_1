# Запуск і оновлення MedLink

Стек: Flask/Gunicorn, PostgreSQL 16, nginx. Код запуску БД винесено в `db.py`, захист і валідацію — у `security.py`.

## Перед першим оновленням на нову версію

1. Збережіть резервну копію наявної БД. Не видаляйте PostgreSQL volume/PVC або namespace.
2. У Secret `medlink-secrets` значення `FLASK_SECRET_KEY` має бути випадковим секретом довжиною щонайменше 32 символи. Нова версія відхиляє короткі та відомі шаблонні ключі. Для генерації власного значення:

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```

   Запишіть його у власний локальний `k8s/01-secrets.yaml` (файл ігнорується Git). Не змінюйте `POSTGRES_PASSWORD`, `POSTGRES_USER` чи `POSTGRES_DB` для існуючого volume: зміна env PostgreSQL не змінює пароль наявного користувача БД.
3. Щоб відкрити реєстрацію лікарів, додайте до `stringData` того ж Secret окремий випадковий `DOCTOR_REGISTRATION_CODE`. Без цього ключа реєстрація нових лікарів закрита; наявні лікарі можуть входити. Передавайте код лише перевіреним лікарям.
4. Нова версія мігрує наявні відкриті паролі до PBKDF2-SHA256 (1 000 000 ітерацій), нормалізує email, переводить дати/час прийомів у типи PostgreSQL DATE/TIME та створює унікальний індекс активних слотів. Усі дії — одна транзакція з блокуванням між Pod-ами. Повторний запуск не перехешовує паролі.
5. Якщо є некоректні дати, дублікати активних слотів або email, які відрізняються лише регістром/пробілами, міграція зупиниться й відкотиться. Дублі не видаляються автоматично: перегляньте логи, виправте конфліктні записи після резервного копіювання, повторіть запуск.
6. На час **першої** міграції зупиніть старі Flask Pod-и, щоб старий код не записував відкриті паролі під час оновлення. Попередній образ несумісний із новим форматом паролів; повернення до нього потребує відновлення резервної копії БД.

Резервна копія для вже запущеного стандартного deployment:

```bash
cd /Users/evgencerednicenko/Desktop/medLink_1
kubectl exec -n medlink deployment/postgres -- pg_dump -U medlink -d medlink --format=custom > medlink-before-update.dump
```

Переконайтеся, що команда успішна і файл не порожній. Для інших DB_USER/DB_NAME підставте власні значення.

## Kubernetes / minikube

Після запуску Docker Desktop:

```bash
cd /Users/evgencerednicenko/Desktop/medLink_1
minikube start --cpus=4 --memory=6g --driver=docker
minikube addons enable ingress
eval "$(minikube docker-env)"
docker build --pull -t medlink-app:latest .
```

Для існуючої інсталяції перед першою міграцією:

```bash
kubectl scale deployment/flask-app -n medlink --replicas=0
kubectl wait --for=delete pod -n medlink -l app=flask-app --timeout=120s
```

Namespace створюйте перед іншими ресурсами. `01-secrets.example.yaml` містить лише приклад, не застосовуйте його замість власного Secret. Ingress Argo CD застосовуйте окремо лише за наявності Argo CD:

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-secrets.yaml
kubectl apply -f k8s/02-postgres-pvc.yaml -f k8s/03-postgres-deployment.yaml -f k8s/04-postgres-service.yaml
kubectl apply -f k8s/05-flask-configmap.yaml -f k8s/06-flask-deployment.yaml -f k8s/07-flask-service.yaml
kubectl apply -f k8s/08-nginx-configmap.yaml -f k8s/09-nginx-deployment.yaml -f k8s/10-nginx-service.yaml
kubectl apply -f k8s/11-medlink-ingress.yaml
kubectl rollout status deployment/flask-app -n medlink --timeout=300s
kubectl get pods -n medlink
```

У Flask deployment знову встановлено 2 репліки. На наступних оновленнях образу з тим самим тегом потрібен `kubectl rollout restart deployment/flask-app -n medlink`, бо `imagePullPolicy: Never` використовує локальний образ.

Відкриття:

```bash
minikube service nginx-service -n medlink
```

Або налаштуйте `medlink.local` для вашого способу доступу до ingress. Для простого доступу без ingress:

```bash
kubectl port-forward -n medlink service/nginx-service 8080:80
```

Відкрийте http://localhost:8080. Якщо використовуєте port-forward/іншу адресу, змініть `PUBLIC_BASE_URL` у `k8s/06-flask-deployment.yaml` на цю адресу **до застосування**, щоб QR-коди вели на доступний сайт. У маніфесті за замовчуванням — `http://medlink.local`.

`/healthz` перевіряє БД і версію міграції (readiness). `/livez` перевіряє Flask (liveness/startup). Startup probe дає до 5 хвилин на ініціалізацію. Контейнер працює без root і без додаткових capabilities.

Логи:

```bash
kubectl logs -n medlink deployment/flask-app --tail=100
kubectl describe pods -n medlink -l app=flask-app
```

Для виключно HTTPS-сайту встановіть `SESSION_COOKIE_SECURE=true`; для локального HTTP залиште `false`. `TRUST_PROXY=true` допустимий лише коли клієнти звертаються через ваш nginx, а не напряму до Gunicorn. Не відкривайте Flask service зовні.

## Docker Compose

```bash
cp .env.example .env
```

Заповніть `.env`: випадковий `FLASK_SECRET_KEY`, `DB_PASSWORD`, за потреби `DOCTOR_REGISTRATION_CODE`. Для наявного `pgdata` використовуйте фактичний пароль БД (попередній Compose задавав `medlink_local_pass`). Не комітьте `.env`.

```bash
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8080/healthz
```

При першій міграції існуючого Compose-стеку спочатку зупиніть старий застосунок (`docker compose stop app nginx`) і зробіть резервну копію PostgreSQL. Зупинка зі збереженням даних: `docker compose down`.

## Тести

Потрібен Python 3.12. Для звичайного запуску тестів без БД:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Тести самі задають окремий тестовий Flask secret. Інтеграційні тести без `TEST_DATABASE_URL` пропускаються. Для них створіть **окрему тестову БД**, задайте її URL у `TEST_DATABASE_URL` і повторіть команду. Тести створюють унікальні схеми та видаляють лише їх. Для PDF на macOS можна задати:

```bash
export PDF_FONT_PATH='/Library/Fonts/Arial Unicode.ttf'
```

GitHub Actions запускає тести з окремим PostgreSQL 16, аудит залежностей і збірку Docker після push/PR. Файл workflow створено локально; до push автоматизація на GitHub не запускається.

## Ручна перевірка після білду

- Відкрити головну, пошук і реєстрацію; перевірити повідомлення про помилки форми.
- Зареєструвати пацієнта, увійти. Пароль із пробілами має працювати без обрізання.
- Зареєструвати лікаря з кодом запрошення; без коду ця дія недоступна.
- Як пацієнт записатися на майбутній будній день, 09:00–16:30, на слот кратний 30 хвилинам.
- Спробувати зайнятий слот іншим пацієнтом: має з'явитися повідомлення, другий запис не створюється.
- Скасувати власний майбутній прийом і повторно забронювати звільнений слот.
- Як лікар відкрити свого пацієнта та додати рекомендації; як пацієнт переглянути історію і завантажити PDF з українським текстом.
- Інший пацієнт/лікар і гість не повинні бачити чужі персональні дані, історію або PDF. QR-посилання також вимагає авторизації.
- Перевірити повідомлення про створення облікового запису, вхід, запис, скасування, збереження історії та вихід.

Повідомлення реалізовані всередині сайту. Email/SMS і push-повідомлення потребують окремого сервісу та налаштувань і в цій версії не відправляються.

## QR на телефоні та новий дизайн (1 жовтня 2026)

Після нового Docker build і перезапуску Flask deployment натисніть «Показати QR»: код з'явиться у вікні, файл скачувати не потрібно. Звичайні приватні QR потребують входу на телефоні з поверненням до вибраної сторінки. Для перегляду без входу явно позначте опцію тимчасового доступу: посилання діє 15 хвилин від створення. Показуйте його лише довіреному отримувачу; закриття вікна не відкликає вже виданий QR.

Для локального Kubernetes-сайту, коли телефон і комп'ютер в одній Wi-Fi мережі:

```bash
sh scripts/phone-preview.sh
# Або вкажіть Wi-Fi IPv4 вручну:
sh scripts/phone-preview.sh 192.168.1.10
```

Скрипт налаштовує `PUBLIC_BASE_URL` в поточному deployment, очікує rollout і відкриває port-forward лише на зазначеній LAN-адресі, порт 8080. Відкрийте надруковану адресу на комп'ютері та телефоні; термінал залишається відкритим. IP у прикладі замініть своїм. Якщо macOS firewall блокує підключення, дозвольте локальний сервер через його штатний запит. В ізольованій/гостьовій Wi-Fi мережі підключення між пристроями може бути заборонене. GitOps може повернути значення з маніфесту — закріпіть LAN-адресу в ньому для стабільного тестування.

Для HTTPS-домену використовуйте його як `PUBLIC_BASE_URL` та ввімкніть `SESSION_COOKIE_SECURE`. Не відкривайте локальний HTTP-перегляд в Інтернет для реальних персональних даних.

Перевірка після запуску: QR лікаря відкриває публічний профіль; приватний QR після входу відкриває відповідну сторінку; тимчасовий QR відкриває лише вибрані дані без входу й відхиляється після 15 хвилин. Перевірте кнопки вікна, Escape, мобільне меню, запис/скасування та читання історії у новому дизайні.
