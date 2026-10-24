FROM ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 AS uv
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME="/home/protodial" \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY --from=uv /uv /bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin protodial \
    && uv sync --locked --no-dev --no-editable \
    && rm -rf /root/.cache/uv

USER protodial
ENTRYPOINT ["protodial"]
