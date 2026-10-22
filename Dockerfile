FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 wta
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY backtest ./backtest
RUN pip install -e ".[dev]"

COPY --chown=wta:wta . .
RUN mkdir -p data logs run && chown -R wta:wta /app

USER wta
# git provenance inside the container (the repo is mounted/copied with .git)
RUN git config --global --add safe.directory /app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "wta", "run", "--mode", "shadow"]
