FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

#tzdata lets the TZ environment variable set the local time used by get_current_date
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

#Install dependencies first so they're cached between code changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agent.py ./

EXPOSE 5005

CMD ["uv", "run", "--no-sync", "agent.py"]
