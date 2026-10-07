#!/bin/sh
# launchd supervises this process; it never starts or changes other Kubernetes contexts.
set -eu
PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
medlink_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
medlink_child=''
stop_child(){ if [ -n "$medlink_child" ]; then kill "$medlink_child" 2>/dev/null || true; wait "$medlink_child" 2>/dev/null || true; medlink_child=''; fi; }
trap 'stop_child; exit 0' INT TERM
while :; do
 medlink_ip=$(/usr/sbin/ipconfig getifaddr en0 2>/dev/null || true)
 medlink_mode=$(cat "$medlink_root/.local-service/mode" 2>/dev/null || echo http)
 case "$medlink_ip" in *[!0-9.]*|'') sleep 15; continue;; esac
 if ! kubectl --context=minikube --request-timeout=10s get service/nginx-service -n medlink >/dev/null 2>&1; then sleep 15; continue; fi
 # Restrict the foreground helper to the user's existing minikube context.
 medlink_context=$(kubectl config current-context 2>/dev/null || true)
 if [ "$medlink_context" != minikube ]; then echo 'Виберіть context minikube для локального MedLink.'; sleep 30; continue; fi
 if [ "$medlink_mode" = https ]; then
  sh "$medlink_root/scripts/local-https.sh" "$medlink_ip" &
 else
  sh "$medlink_root/scripts/phone-preview.sh" "$medlink_ip" &
 fi
 medlink_child=$!
 while kill -0 "$medlink_child" 2>/dev/null; do
  sleep 10
  medlink_new_ip=$(/usr/sbin/ipconfig getifaddr en0 2>/dev/null || true)
  medlink_new_mode=$(cat "$medlink_root/.local-service/mode" 2>/dev/null || echo http)
  if [ "$medlink_new_ip" != "$medlink_ip" ] || [ "$medlink_new_mode" != "$medlink_mode" ]; then break; fi
 done
 stop_child
 sleep 5
done
