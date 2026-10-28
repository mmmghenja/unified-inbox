# Демо-образ: ядро хаба на выдуманных данных (demo/serve.py), интерфейс собран с VITE_SHOWCASE=1.
# Сборка:  docker compose build     Запуск:  docker compose up     Подробности — README.md, раздел «Быстрый старт: демо в Docker».

# --- 1. интерфейс -------------------------------------------------------------------------------------------
FROM node:24-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
ENV VITE_SHOWCASE=1
RUN npx vite build

# --- 2. зависимости Python и код ------------------------------------------------------------------------------
FROM python:3.14-slim AS py
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY demo ./demo
RUN uv sync --frozen --no-dev

# --- 3. рабочий образ: без uv, без исходников интерфейса, без тестов, под обычным пользователем -----------------
FROM python:3.14-slim
RUN useradd --system --uid 10001 --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin hub \
    && mkdir /data /config \
    && chown hub:hub /data /config
COPY --from=py /app /app
COPY --from=ui /ui/dist /app/ui/dist
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    HUB_DATA_DIR=/data \
    HUB_CONFIG_DIR=/config \
    HUB_DEMO_PORT=8787 \
    HUB_DEMO_LISTEN_PORT=8080
USER hub
VOLUME ["/data", "/config"]
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 CMD ["python", "-m", "demo.serve", "health"]
CMD ["python", "-m", "demo.serve"]
