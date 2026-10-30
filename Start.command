#!/bin/sh
cd -- "$(dirname -- "$0")" || exit 1
# Finder does not inherit a terminal's complete PATH.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
sh ./start.sh
result=$?
if [ "$result" != 0 ]; then
    printf '\n%s\n' 'See readme.md for mise trust and setup. Press Return to close.'
    read -r reply
fi
exit "$result"
