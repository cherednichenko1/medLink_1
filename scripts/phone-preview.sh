#!/bin/sh
# Run after building/applying the new image. No DB credentials are changed.
set -eu
medlink_ip=${1:-}
if [ -z "$medlink_ip" ]; then
  medlink_ip=$(/usr/sbin/ipconfig getifaddr en0 2>/dev/null || true)
fi
if [ -z "$medlink_ip" ]; then
  echo 'Не знайдено Wi-Fi IP. Запустіть: sh scripts/phone-preview.sh 192.168.x.x' >&2
  exit 1
fi
case "$medlink_ip" in
  *[!0-9.]*|'') echo 'Потрібна IPv4-адреса комп’ютера у вашій локальній мережі.' >&2; exit 1 ;;
esac
medlink_url="http://$medlink_ip:8081"
echo "Телефон і комп’ютер мають бути в одній мережі Wi-Fi. Адреса: $medlink_url"
kubectl set env -n medlink deployment/flask-app "PUBLIC_BASE_URL=$medlink_url"
kubectl rollout status -n medlink deployment/flask-app --timeout=300s
echo 'Відкрийте цю адресу на комп’ютері й телефоні. Тримайте термінал відкритим; Ctrl+C зупиняє доступ.'
exec kubectl port-forward -n medlink --address="$medlink_ip" service/nginx-service 8081:80
