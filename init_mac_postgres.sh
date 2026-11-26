#!/usr/bin/env bash
set -euo pipefail
umask 077

# Install the server first with: brew install postgresql@16
pg_bin="$(brew --prefix postgresql@16)/bin"
if [[ ! -x "$pg_bin/postgres" ]]; then
  printf 'Install the PostgreSQL 16 server with brew install postgresql@16.\n' >&2
  exit 1
fi

state_dir="${HARVEST_LOCAL_ROOT:-$HOME}/.harvest-log"
data_dir="$state_dir/postgres16"
socket_dir="$state_dir/run"
mkdir -p "$socket_dir"
chmod 700 "$state_dir" "$socket_dir"

if [[ ! -f "$data_dir/PG_VERSION" ]]; then
  # Only this macOS account can use these two roles on the private socket.
  mac_user="$(id -un)"
  printf 'local all harvest_admin,harvest_app peer map=harvest\n' > "$state_dir/pg_hba.conf"
  printf 'harvest "%s" harvest_admin\nharvest "%s" harvest_app\n' \
    "$mac_user" "$mac_user" > "$state_dir/pg_ident.conf"
  "$pg_bin/initdb" -D "$data_dir" --username=harvest_admin --encoding=UTF8 --locale=C \
    --auth-local=peer --auth-host=scram-sha-256 \
    --set=listen_addresses= --set=port=5432 --set="unix_socket_directories=$socket_dir" \
    --set="hba_file=$state_dir/pg_hba.conf" --set="ident_file=$state_dir/pg_ident.conf"
fi

if [[ "$(cat "$data_dir/PG_VERSION")" != 16 ]]; then
  printf 'Expected PostgreSQL 16 in %s.\n' "$data_dir" >&2
  exit 1
fi
if ! "$pg_bin/pg_ctl" -D "$data_dir" status >/dev/null; then
  "$pg_bin/pg_ctl" -D "$data_dir" -l "$state_dir/postgres.log" -w start
fi

client=("$pg_bin/psql" -X -w --set=ON_ERROR_STOP=1 --host="$socket_dir" --port=5432)
"${client[@]}" --username=harvest_admin --dbname=postgres <<'SQL'
SELECT 'CREATE ROLE harvest_app LOGIN NOSUPERUSER NOCREATEROLE CREATEDB'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'harvest_app') \gexec
SELECT 'CREATE DATABASE harvest_log OWNER harvest_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'harvest_log') \gexec
SQL

"${client[@]}" --username=harvest_app --dbname=harvest_log \
  --command="SELECT current_database(), current_user, current_setting('server_version') AS server_version;"
