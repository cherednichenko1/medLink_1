#!/bin/sh
cd "$(dirname "$0")" || exit 1
sh scripts/local-https.sh
read -r medlink_finished
