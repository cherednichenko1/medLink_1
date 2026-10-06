#!/bin/sh
# Explicitly publishes this application through Cloudflare. Run only after reviewing DEPLOY.md.
set -eu
if [ "${1:-}" != '--publish' ]; then
  echo 'Ця команда відкриває MedLink через Cloudflare, який оброблятиме HTTP-запити застосунку.' >&2
  echo 'Для свідомого запуску після перегляду DEPLOY.md: sh scripts/internet-preview.sh --publish' >&2
  exit 2
fi
for medlink_tool in kubectl docker curl; do
  command -v "$medlink_tool" >/dev/null || { echo "Потрібен $medlink_tool" >&2; exit 1; }
done
medlink_temp=$(mktemp -d "${TMPDIR:-/tmp}/medlink-internet.XXXXXX")
medlink_container="medlink-https-$(date +%s)-$$"
medlink_url=''
medlink_pf_pid=''
medlink_previous_url=$(kubectl get deployment/flask-app -n medlink -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PUBLIC_BASE_URL")].value}')
medlink_previous_secure=$(kubectl get deployment/flask-app -n medlink -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SESSION_COOKIE_SECURE")].value}')
medlink_config_changed=false
cleanup() {
  trap - EXIT INT TERM
  docker rm --force "$medlink_container" >/dev/null 2>&1 || true
  if [ -n "$medlink_pf_pid" ]; then kill "$medlink_pf_pid" 2>/dev/null || true; fi
  if [ "$medlink_config_changed" = true ]; then
    medlink_current_url=$(kubectl get deployment/flask-app -n medlink -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PUBLIC_BASE_URL")].value}' 2>/dev/null || true)
    if [ "$medlink_current_url" = "$medlink_url" ]; then
      if [ -n "$medlink_previous_url" ]; then
        kubectl set env -n medlink deployment/flask-app "PUBLIC_BASE_URL=$medlink_previous_url" "SESSION_COOKIE_SECURE=${medlink_previous_secure:-false}" >/dev/null || true
      else
        kubectl set env -n medlink deployment/flask-app PUBLIC_BASE_URL- "SESSION_COOKIE_SECURE=${medlink_previous_secure:-false}" >/dev/null || true
      fi
    fi
  fi
  rm -rf "$medlink_temp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
kubectl port-forward -n medlink --address=127.0.0.1 service/nginx-service 8082:80 >"$medlink_temp/forward.log" 2>&1 &
medlink_pf_pid=$!
medlink_ready=false
for medlink_attempt in 1 2 3 4 5 6 7 8 9 10; do
  kill -0 "$medlink_pf_pid" 2>/dev/null || { echo 'Не вдалося запустити port-forward; можливо, порт 8082 зайнято.' >&2; exit 1; }
  if curl --silent --fail --max-time 2 http://127.0.0.1:8082/livez >/dev/null; then medlink_ready=true; break; fi
  sleep 1
done
[ "$medlink_ready" = true ] || { echo 'MedLink не відповідає локально.' >&2; exit 1; }
curl --silent --show-error --connect-timeout 5 --max-time 10 --output /dev/null https://api.trycloudflare.com/tunnel || { echo 'API Cloudflare недоступний з цього інтернет-підключення. Спробуйте іншу мережу.' >&2; exit 1; }
docker run --detach --name "$medlink_container" cloudflare/cloudflared@sha256:9b49eed8f62806d5d45ddf59ecefb5710429598ea6d3fcccd2af938f621b2b07 tunnel --no-autoupdate --url http://host.docker.internal:8082 --protocol http2 >/dev/null
medlink_attempt=0
while [ "$medlink_attempt" -lt 60 ]; do
  docker logs "$medlink_container" >"$medlink_temp/tunnel.log" 2>&1 || true
  medlink_url=$(sed -nE 's/.*(https:\/\/[a-z0-9-]+\.trycloudflare\.com).*/\1/p' "$medlink_temp/tunnel.log" | head -n 1)
  [ -z "$medlink_url" ] || break
  if [ "$(docker inspect --format '{{.State.Running}}' "$medlink_container" 2>/dev/null || true)" != true ]; then
    echo 'Cloudflare завершився до отримання адреси:' >&2
    tail -n 5 "$medlink_temp/tunnel.log" >&2
    exit 1
  fi
  medlink_attempt=$((medlink_attempt + 1)); sleep 1
done
[ -n "$medlink_url" ] || { echo 'Cloudflare не видав HTTPS-адресу. Повторіть запуск пізніше.' >&2; exit 1; }
medlink_config_changed=true
kubectl set env -n medlink deployment/flask-app "PUBLIC_BASE_URL=$medlink_url" SESSION_COOKIE_SECURE=true
kubectl rollout status -n medlink deployment/flask-app --timeout=300s
curl --silent --fail --retry 5 --retry-delay 2 --max-time 15 "$medlink_url/livez" >/dev/null || { echo 'HTTPS-тунель не відповідає; параметри буде повернуто.' >&2; exit 1; }
echo "MedLink через інтернет: $medlink_url"
echo 'Відкрийте цю HTTPS-адресу на комп’ютері, увійдіть і створіть НОВИЙ QR. Телефон може бути на мобільному інтернеті.'
echo 'Тримайте Mac, Docker Desktop і цей термінал увімкненими. Ctrl+C зупиняє тунель та повертає попередню адресу.'
while kill -0 "$medlink_pf_pid" 2>/dev/null && [ "$(docker inspect --format '{{.State.Running}}' "$medlink_container" 2>/dev/null || true)" = true ]; do sleep 5; done
echo 'Тунель або port-forward завершився. Запустіть команду знову для нової адреси.' >&2
exit 1
