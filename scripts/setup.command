#!/bin/bash
set -euo pipefail
NHIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$NHIS_PROJECT_ROOT"
NHIS_PYTHON="${NHIS_PYTHON:-python3}"
printf '%s\n' 'NHIS Lab · Install the project environment and download official CDC data'
"$NHIS_PYTHON" -c 'import sys; assert sys.version_info >= (3,9), "Python 3.9 or newer is required"'
if [[ ! -x "$NHIS_PROJECT_ROOT/.venv/bin/python" ]]; then
    "$NHIS_PYTHON" -m venv "$NHIS_PROJECT_ROOT/.venv"
fi
NHIS_PYTHON="$NHIS_PROJECT_ROOT/.venv/bin/python"
"$NHIS_PYTHON" -m pip install -r "$NHIS_PROJECT_ROOT/requirements.txt"
NHIS_R_READY=0
if [[ -x "$NHIS_PROJECT_ROOT/.r-env/bin/Rscript" ]]; then
    if "$NHIS_PROJECT_ROOT/.r-env/bin/Rscript" -e 'stopifnot(all(vapply(c("survey","jsonlite","mitools"), requireNamespace, logical(1), quietly=TRUE)))' >/dev/null 2>&1; then
        NHIS_R_READY=1
    fi
elif command -v Rscript >/dev/null 2>&1; then
    if Rscript -e 'stopifnot(all(vapply(c("survey","jsonlite","mitools"), requireNamespace, logical(1), quietly=TRUE)))' >/dev/null 2>&1; then
        NHIS_R_READY=1
    fi
fi
if [[ "$NHIS_R_READY" -ne 1 ]]; then
    NHIS_CONDA="$(command -v conda || true)"
    if [[ -z "$NHIS_CONDA" && -x "$HOME/miniconda3/bin/conda" ]]; then
        NHIS_CONDA="$HOME/miniconda3/bin/conda"
    fi
    if [[ -z "$NHIS_CONDA" ]]; then
        printf '%s\n' 'R with survey/jsonlite/mitools or conda was not found. Install R from https://cran.r-project.org and these three packages, then rerun this script.'
        exit 1
    fi
    printf '%s\n' 'Install isolated R, survey, jsonlite, and mitools under the project .r-env directory.'
    "$NHIS_CONDA" create --prefix "$NHIS_PROJECT_ROOT/.r-env" --override-channels -c conda-forge r-base r-survey r-jsonlite r-mitools -y
fi
if [[ -x "$NHIS_PROJECT_ROOT/.r-env/bin/Rscript" ]]; then
    export PATH="$NHIS_PROJECT_ROOT/.r-env/bin:$PATH"
fi
"$NHIS_PYTHON" "$NHIS_PROJECT_ROOT/scripts/download_data.py"
printf '%s\n' 'Real data and the statistical environment are ready. Starting NHIS Lab.' 'The model connection is managed through the server .env file.'
if [[ "${1:-}" != "--no-launch" ]]; then
    exec /bin/bash "$NHIS_PROJECT_ROOT/scripts/start.command"
fi
