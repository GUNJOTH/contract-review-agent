FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    tzdata \
    libmupdf-dev \
    mupdf-tools \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml .
COPY uv.lock .
COPY src/ src/
COPY data/ data/

RUN uv venv /app/.venv --python 3.11 && \
    uv sync --frozen --no-dev --python /app/.venv/bin/python

EXPOSE 8090

RUN mkdir -p /app/runtime/tasks/input /app/runtime/tasks/output /app/runtime/tasks/tmp /app/runtime/celery /app/runtime/review_cache /app/runtime/embedding_cache

CMD ["/app/.venv/bin/python", "-m", "contract_review_app.bootstrap"]
