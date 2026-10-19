#!/bin/sh
# ATSC scan web UI — http://<server>:8088/
cd "$(dirname "$0")" && exec python3 backend/server.py "$@"
