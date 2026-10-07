#!/bin/sh
set -eu
medlink_mode=${1:-http}
case "$medlink_mode" in http|https) ;; *) echo 'Режим: http або https' >&2; exit 1;; esac
medlink_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
medlink_label=com.medlink.medlink-1.local
medlink_agents="$HOME/Library/LaunchAgents"
medlink_plist="$medlink_agents/$medlink_label.plist"
medlink_runtime="$HOME/Library/Application Support/MedLink Local"
mkdir -p "$medlink_runtime/scripts" "$medlink_runtime/.local-service" "$medlink_agents"
for medlink_script in local-service.sh phone-preview.sh local-https.sh; do cp "$medlink_root/scripts/$medlink_script" "$medlink_runtime/scripts/$medlink_script"; done
# Preserve the existing local CA when switching HTTPS to background mode.
if [ -d "$medlink_root/.local-tls" ] && [ ! -f "$medlink_runtime/.local-tls/ca.key" ]; then
 mkdir -p "$medlink_runtime/.local-tls"
 cp -p "$medlink_root/.local-tls/"* "$medlink_runtime/.local-tls/"
fi
printf '%s\n' "$medlink_mode" > "$medlink_runtime/.local-service/mode"
# plistlib safely escapes the repository path, including spaces and XML characters.
/usr/bin/python3 - "$medlink_runtime" "$medlink_plist" "$medlink_label" <<'PY'
import plistlib,sys
from pathlib import Path
root,path,label=sys.argv[1:]
config={'Label':label,'ProgramArguments':['/bin/sh',root+'/scripts/local-service.sh'],'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,'ProcessType':'Background','StandardOutPath':root+'/.local-service/agent.log','StandardErrorPath':root+'/.local-service/agent.err'}
with open(path,'wb') as f:plistlib.dump(config,f)
PY
medlink_uid=$(id -u)
launchctl bootout "gui/$medlink_uid/$medlink_label" 2>/dev/null || true
launchctl bootstrap "gui/$medlink_uid" "$medlink_plist"
echo 'Фоновий MedLink увімкнено. Terminal можна закривати. Mac, Docker Desktop і minikube мають працювати.'
echo "Режим: $medlink_mode. Логи: $medlink_runtime/.local-service/agent.log"
