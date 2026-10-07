#!/bin/sh
set -eu
medlink_label=com.medlink.medlink-1.local
launchctl bootout "gui/$(id -u)/$medlink_label" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$medlink_label.plist"
echo 'Фоновий локальний MedLink вимкнено.'
