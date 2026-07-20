FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
RUN corepack enable && corepack prepare pnpm@11.9.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/tsconfig.json ./
COPY frontend/src ./src
RUN pnpm run build

FROM python:3.13-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend-build /app/frontend/static/app.js /app/frontend/static/app.js
COPY --from=frontend-build /app/frontend/static/app.js.map /app/frontend/static/app.js.map
COPY --from=frontend-build /app/frontend/static/discovery.js /app/frontend/static/discovery.js
COPY --from=frontend-build /app/frontend/static/discovery.js.map /app/frontend/static/discovery.js.map
EXPOSE 5056
CMD ["gunicorn", "--config=backend/gunicorn.conf.py", "backend.app:app"]
