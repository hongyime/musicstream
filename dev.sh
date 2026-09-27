#!/usr/bin/env sh
# Dev only: explicit build, then source edits use up --no-build.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$repo_dir"
action=${1:-up}
if [ "$#" -gt 0 ]; then shift; fi
case "$action" in
    up)
        for argument in "$@"; do
            case "$argument" in
                --build*|--no-build*|--pull*)
                    printf '%s\n' 'Use the explicit build action for dependency changes; pulling is a separate command.' >&2
                    exit 2
                    ;;
            esac
        done
        exec docker compose --env-file .env.dev -f compose.dev.yaml up --no-build "$@"
        ;;
    build|down|logs|ps)
        exec docker compose --env-file .env.dev -f compose.dev.yaml "$action" "$@"
        ;;
    *)
        printf '%s\n' 'Usage: sh dev.sh [up|build|down|logs|ps] [compose command arguments]' >&2
        exit 2
        ;;
esac
