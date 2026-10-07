#!/bin/sh
# Local HTTPS only. Device trust must be configured by the user, never silently installed.
set -eu
umask 077
medlink_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
medlink_ip=${1:-$(/usr/sbin/ipconfig getifaddr en0 2>/dev/null || true)}
case "$medlink_ip" in *[!0-9.]*|'') echo 'Вкажіть локальну IPv4 адресу Mac.' >&2; exit 1;; esac
medlink_tls="$medlink_root/.local-tls"
mkdir -p "$medlink_tls"
chmod 755 "$medlink_tls"
if [ ! -f "$medlink_tls/ca.key" ]; then
 openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 -keyout "$medlink_tls/ca.key" -out "$medlink_tls/medlink-ca.crt" -subj '/CN=MedLink Local Development CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign'
fi
openssl req -newkey rsa:2048 -nodes -keyout "$medlink_tls/server.key" -out "$medlink_tls/server.csr" -subj '/CN=MedLink Local'
cat > "$medlink_tls/server.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$medlink_ip,IP:127.0.0.1,DNS:localhost
EOF
openssl x509 -req -in "$medlink_tls/server.csr" -CA "$medlink_tls/medlink-ca.crt" -CAkey "$medlink_tls/ca.key" -CAcreateserial -out "$medlink_tls/server.crt" -days 90 -sha256 -extfile "$medlink_tls/server.ext"
chmod 644 "$medlink_tls/medlink-ca.crt" "$medlink_tls/server.crt"
cat > "$medlink_tls/nginx.conf" <<EOF
log_format medlink_safe '\$request_method \$uri \$status';
server {
 listen 80;
 access_log /var/log/nginx/access.log medlink_safe;
 location = /medlink-ca.crt { alias /certs/medlink-ca.crt; default_type application/x-x509-ca-cert; }
 location / { return 404; }
}
server {
 listen 443 ssl;
 ssl_certificate /certs/server.crt;
 ssl_certificate_key /certs/server.key;
 ssl_protocols TLSv1.2 TLSv1.3;
 access_log /var/log/nginx/access.log medlink_safe;
 client_max_body_size 6m;
 location / {
  proxy_pass http://host.docker.internal:8082;
  proxy_set_header Host \$host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_read_timeout 60s;
 }
}
EOF
medlink_container="medlink-local-https-$$"
medlink_pid=''
cleanup() {
 trap - EXIT INT TERM
 medlink_current_url=$(kubectl get deployment/flask-app -n medlink -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PUBLIC_BASE_URL")].value}' 2>/dev/null || true)
 if [ "$medlink_current_url" = "https://$medlink_ip:8443" ]; then kubectl set env -n medlink deployment/flask-app "PUBLIC_BASE_URL=http://$medlink_ip:8081" SESSION_COOKIE_SECURE=false >/dev/null || true; fi
 docker rm --force "$medlink_container" >/dev/null 2>&1 || true
 if [ -n "$medlink_pid" ]; then kill "$medlink_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
kubectl port-forward -n medlink --address=127.0.0.1 service/nginx-service 8082:80 &
medlink_pid=$!
sleep 2
kill -0 "$medlink_pid" 2>/dev/null || { echo 'Порт 8082 зайнятий або Kubernetes недоступний.' >&2; exit 1; }
docker run --detach --name "$medlink_container" -p "$medlink_ip:8442:80" -p "$medlink_ip:8443:443" -v "$medlink_tls:/certs:ro" -v "$medlink_tls/nginx.conf:/etc/nginx/conf.d/default.conf:ro" nginx:alpine >/dev/null
medlink_tls_ready=false
for medlink_attempt in 1 2 3 4 5 6 7 8 9 10; do
 if curl --silent --fail --max-time 5 --cacert "$medlink_tls/medlink-ca.crt" "https://$medlink_ip:8443/livez" >/dev/null; then medlink_tls_ready=true; break; fi
 sleep 1
done
if [ "$medlink_tls_ready" != true ]; then echo 'Локальний HTTPS не відповідає.' >&2; docker logs "$medlink_container" >&2; exit 1; fi
if [ "${2:-}" = '--check' ]; then echo 'Local TLS certificate and proxy: OK (application URL unchanged)'; exit 0; fi
kubectl set env -n medlink deployment/flask-app "PUBLIC_BASE_URL=https://$medlink_ip:8443" SESSION_COOKIE_SECURE=true
kubectl rollout status -n medlink deployment/flask-app --timeout=300s
echo "Сертифікат для iPhone: http://$medlink_ip:8442/medlink-ca.crt"
echo "MedLink HTTPS: https://$medlink_ip:8443"
echo 'Встановіть і явно довірте тільки цьому локальному сертифікату за DEPLOY.md, тоді відкрийте HTTPS і створіть новий QR.'
echo 'Не передавайте ca.key або server.key. Тримайте термінал відкритим. Ctrl+C зупиняє локальний HTTPS.'
while kill -0 "$medlink_pid" 2>/dev/null && [ "$(docker inspect --format '{{.State.Running}}' "$medlink_container" 2>/dev/null || true)" = true ]; do sleep 5; done
exit 1
