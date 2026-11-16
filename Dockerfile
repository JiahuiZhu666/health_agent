# syntax=docker/dockerfile:1
# Verified official-images source: docker-library/python commit
# 83054588043b3a7ac1ce8324c9242164b632beb9, 3.11/slim-bookworm.
# CI resolves this exact tag to a registry digest and passes PYTHON_BASE.
# A reviewed digest in source and full transitive OS lock remain release gates.
ARG PYTHON_BASE=python:3.11.18-slim-bookworm
FROM ${PYTHON_BASE} AS runtime-base
ARG PYTHON_BASE

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NHIS_HOST=0.0.0.0 \
    NHIS_STATE_DIR=/var/lib/nhis \
    NHIS_AUTO_DOWNLOAD_DATA=false

# Exact direct versions verified against packages.debian.org/bookworm.
# Security/transitive dependencies still come from the signed Debian repository;
# record their actual versions and rerun all numerical gates for any update.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        r-base-core=4.2.2.20221110-2 \
        r-cran-jsonlite=1.8.4+dfsg-1 \
        r-cran-mitools=2.4-2 \
        r-cran-survey=4.1-1-1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 nhis \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent nhis \
    && mkdir -p /var/lib/nhis /usr/share/nhis-provenance \
    && chown nhis:nhis /var/lib/nhis \
    && chmod 0700 /var/lib/nhis \
    && dpkg-query -W > /usr/share/nhis-provenance/debian-packages.tsv \
    && python --version > /usr/share/nhis-provenance/python-version.txt \
    && Rscript --vanilla -e 'stopifnot(all(vapply(c("survey", "jsonlite", "mitools"), requireNamespace, logical(1), quietly=TRUE))); print(sessionInfo())' > /usr/share/nhis-provenance/r-session.txt

LABEL org.opencontainers.image.base.name="${PYTHON_BASE}"
WORKDIR /app

# This stage executes real R only against deterministic synthetic test inputs.
# Its fixture generator and data are never copied into the runtime image.
FROM runtime-base AS validation
COPY config/registry.json ./config/registry.json
COPY scripts/analyze.R scripts/validate_fixture.py ./scripts/
COPY tests/fixtures/generate_survey.py tests/fixtures/reference_survey.R tests/fixtures/expected_metadata.json ./tests/fixtures/
RUN mkdir /validation \
    && python scripts/validate_fixture.py --rscript /usr/bin/Rscript --output /validation/fixture-validation.json > /validation/fixture-validation.stdout.json

# Separate allowlist: production cannot access a synthetic-data fallback.
FROM runtime-base AS runtime
COPY server.py ./
COPY nhis_agent/ ./nhis_agent/
COPY web/ ./web/
COPY config/registry.json ./config/registry.json
COPY scripts/analyze.R scripts/reference.do scripts/state_admin.py scripts/model_admin.py ./scripts/
COPY deploy/entrypoint.py ./deploy/entrypoint.py
# Depending on this artifact makes the real-R fixture mandatory for a build.
COPY --from=validation /validation/fixture-validation.json /usr/share/nhis-provenance/fixture-validation.json

EXPOSE 10000
STOPSIGNAL SIGTERM
# Startup prepares a new mounted directory and drops to UID/GID 10001.
# API credentials are runtime-only; no real secret is a build ARG or image ENV.
CMD ["python", "deploy/entrypoint.py"]
