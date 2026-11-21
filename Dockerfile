# bookworm (OpenSSL 3.0) rather than trixie: some networks drop the larger post-quantum TLS
# ClientHello that newer OpenSSL sends, which breaks pip downloads during the build.
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY mocklab ./mocklab
COPY migrations ./migrations
RUN useradd --create-home --uid 10001 appuser
USER appuser

FROM runtime AS dev
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY pyproject.toml ./
COPY tests ./tests
COPY scripts ./scripts
USER appuser
