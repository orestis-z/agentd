#!/bin/bash
set -e

if [ -d /repos-cache ] && [ -z "$(ls -A /workspace 2>/dev/null)" ]; then
    cp -a /repos-cache/* /workspace/
fi

exec /entrypoint.sh "$@"
