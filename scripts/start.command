#!/bin/bash
set -euo pipefail
NHIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$NHIS_PROJECT_ROOT"
NHIS_PYTHON="${NHIS_PYTHON:-python3}"
if [[ -x "$NHIS_PROJECT_ROOT/.venv/bin/python" ]]; then
    NHIS_PYTHON="$NHIS_PROJECT_ROOT/.venv/bin/python"
fi
if [[ -x "$NHIS_PROJECT_ROOT/.r-env/bin/Rscript" ]]; then
    export PATH="$NHIS_PROJECT_ROOT/.r-env/bin:$PATH"
fi
case "${1:---web}" in
    --help|-h)
        printf '%s\n' 'Usage: bash scripts/start.command [--web|--streamlit|--test-api]' 'Default: chat web app. --test-api reads the server .env and makes one real connection request.'
        exit 0
        ;;
    --web)
        printf '%s\n' 'NHIS Lab · Conversation and survey research' 'Open: http://127.0.0.1:8765' 'Stop: press Control-C in this terminal'
        exec "$NHIS_PYTHON" "$NHIS_PROJECT_ROOT/server.py" --port 8765 --open
        ;;
    --streamlit)
        printf '%s\n' 'NHIS Lab · Streamlit interface' 'Open: http://127.0.0.1:8765' 'Stop: press Control-C in this terminal'
        exec "$NHIS_PYTHON" -m streamlit run "$NHIS_PROJECT_ROOT/streamlit_app.py" --server.address 127.0.0.1 --server.port 8765
        ;;
    --test-api)
        exec "$NHIS_PYTHON" "$NHIS_PROJECT_ROOT/server.py" --test-api
        ;;
    *)
        printf '%s\n' 'Unknown option. Use --web, --streamlit, --test-api, or --help.' >&2
        exit 2
        ;;
esac
