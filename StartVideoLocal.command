#!/bin/sh
cd "$(dirname "$0")" || exit 1
sh scripts/install-local-service.sh https
