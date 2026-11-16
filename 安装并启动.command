#!/bin/bash
set -euo pipefail
NHIS_LAUNCH_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$NHIS_LAUNCH_ROOT/scripts/setup.command" "$@"
