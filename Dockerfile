FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
RUN corepack enable && corepack prepare pnpm@11.9.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
COPY frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/tsconfig.json ./
COPY frontend/src ./src
COPY frontend/scripts ./scripts
COPY frontend/static ./static
RUN pnpm run build

FROM python:3.13-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN apt-get update \
    && apt-get install --no-install-recommends --yes passwd \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 melodarr \
    && useradd \
        --no-create-home \
        --no-log-init \
        --uid 1000 \
        --gid 1000 \
        --home-dir /app/data \
        --shell /usr/sbin/nologin \
        melodarr \
    && mkdir -p /app/data \
    && chown 1000:1000 /app/data
COPY . .
COPY --from=frontend-build /app/frontend/static /app/frontend/static
ENV HOME=/app/data \
    MELODARR_DATABASE=/app/data/melodarr.db \
    MELODARR_CACHE_DATABASE=/app/data/cache/metadata.db
EXPOSE 5056
USER melodarr:melodarr
CMD ["gunicorn", "--chdir=/app", "--config=/app/backend/gunicorn.conf.py", "backend.app:app"]
