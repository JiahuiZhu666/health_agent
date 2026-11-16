# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NHIS_HOST=0.0.0.0 \
    NHIS_STATE_DIR=/var/lib/nhis \
    NHIS_AUTO_DOWNLOAD_DATA=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        r-base-core \
        r-cran-jsonlite \
        r-cran-mitools \
        r-cran-survey \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 nhis \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent nhis \
    && mkdir -p /var/lib/nhis \
    && chown nhis:nhis /var/lib/nhis \
    && Rscript --vanilla -e 'stopifnot(all(vapply(c("survey", "jsonlite", "mitools"), requireNamespace, logical(1), quietly=TRUE)))'

WORKDIR /app
# Copy only runtime source. Local credentials, CSVs and environments are excluded
# independently by the allowlist in .dockerignore.
COPY server.py ./
COPY nhis_agent/ ./nhis_agent/
COPY web/ ./web/
COPY config/registry.json ./config/registry.json
COPY scripts/analyze.R scripts/reference.do ./scripts/
COPY deploy/entrypoint.py ./deploy/entrypoint.py

EXPOSE 10000
# The entrypoint prepares a newly mounted disk and immediately drops privileges.
# No API secret is an ARG/ENV in this image or required during the build.
CMD ["python", "deploy/entrypoint.py"]
