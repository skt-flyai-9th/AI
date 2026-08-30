#!/bin/sh
set -eu
mkdir -p "${RANKER_DATA_DIR:-runtime-data}" "${EXPORT_DIR:-exports}"
if [ "${RUN_DB_MIGRATIONS:-true}" = "true" ]; then
  alembic upgrade head
fi
ai-service purge-removed-shortforms
exec "$@"
