#!/bin/sh
set -eu
exec minio server /data --address ":${PORT:-9000}"
