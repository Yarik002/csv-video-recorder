#!/bin/sh
# Starts Apache in the foreground as the main process of the web container.
set -e
. /etc/apache2/envvars
mkdir -p "$APACHE_RUN_DIR" "$APACHE_LOCK_DIR" "$APACHE_LOG_DIR"
# Left over when the container was stopped and is started again (docker compose stop/start).
rm -f "$APACHE_PID_FILE"
exec apache2 -D FOREGROUND
