#!/bin/sh
set -eu
mkdir -p "${RANKER_DATA_DIR:-runtime-data}" "${EXPORT_DIR:-exports}"
alembic upgrade head
exec "$@"
