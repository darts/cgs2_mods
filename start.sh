#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
if ! command -v mise >/dev/null 2>&1; then
    printf '%s\n' 'Install mise first: https://mise.jdx.dev/installing-mise.html' 'Then follow the three commands in readme.md.'
    exit 1
fi
exec mise run start
