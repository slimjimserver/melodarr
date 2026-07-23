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
    && apt-get install --no-install-recommends --yes gosu passwd \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend-build /app/frontend/static /app/frontend/static
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh
ENV PUID=1000 \
    PGID=1000 \
    HOME=/app/data \
    MELODARR_DATABASE=/app/data/melodarr.db \
    MELODARR_CACHE_DATABASE=/app/data/cache/metadata.db
EXPOSE 5056
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["gunicorn", "--config=backend/gunicorn.conf.py", "backend.app:app"]
