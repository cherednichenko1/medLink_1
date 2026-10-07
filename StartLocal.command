#!/bin/sh
cd "$(dirname "$0")" || exit 1
sh scripts/phone-preview.sh
read -r medlink_finished
