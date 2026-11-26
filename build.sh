#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
phase="${1:-all}"
case "$phase" in
  build|release|all) ;;
  *) printf 'Usage: bash build.sh [build|release|all]\n' >&2; exit 2 ;;
esac

required_python=3.13.18
actual_python="$(python -c 'import platform; print(platform.python_implementation(), platform.python_version())')"
if [[ "$actual_python" != "CPython $required_python" ]]; then
  printf 'Harvest Log requires CPython %s. Found %s.\n' "$required_python" "$actual_python" >&2
  exit 1
fi
: "${DJANGO_SECRET_KEY:?Set DJANGO_SECRET_KEY in the service environment.}"
: "${DATABASE_URL:?Set DATABASE_URL to the PostgreSQL connection URL.}"
case "$DATABASE_URL" in
  postgres://*|postgresql://*) ;;
  *) printf 'DATABASE_URL must use a PostgreSQL URL.\n' >&2; exit 2 ;;
esac

if [[ "$phase" == release || "$phase" == all ]]; then
  case "${SEED_DEMO:-0}" in
    0|1) ;;
    *) printf 'SEED_DEMO must be 0 or 1.\n' >&2; exit 2 ;;
  esac
fi

if [[ "$phase" == build || "$phase" == all ]]; then
  python -m pip install -r requirements.txt
  python -m pip check
  python manage.py collectstatic --noinput
fi

if [[ "$phase" == release || "$phase" == all ]]; then
  python manage.py migrate --noinput
  if [[ "${SEED_DEMO:-0}" == 1 ]]; then
    # Enable once the idempotent seed_demo management command ships.
    python manage.py seed_demo
  fi
fi
