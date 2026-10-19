#!/bin/bash
set -euo pipefail
export PGPORT="${PORT:-5432}"
exec docker-entrypoint.sh postgres
