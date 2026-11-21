FROM busybox:1.37.0-musl AS probe

FROM python:3.12.14-slim-bookworm AS agent
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt && pip check
COPY agent agent
COPY agent_logger agent_logger
COPY clients clients
COPY db db
COPY investigation investigation
COPY knowledge knowledge
COPY migrations migrations
COPY model_client model_client
COPY models models
COPY monitor monitor
COPY prompts prompts
COPY recovery recovery
COPY remediation remediation
COPY services services
COPY tools tools
COPY web_api web_api
COPY alembic.ini alert_*.py config.py fingerprint.py incident_factory.py incident_store.py main.py reinvestigation.py runtime_cli.py storage_lock.py ./

FROM python:3.12.14-slim-bookworm AS order-service
WORKDIR /srv/order
ENV PYTHONUNBUFFERED=1
COPY deploy/order-requirements.lock.txt .
RUN pip install --no-cache-dir -r order-requirements.lock.txt && pip check
COPY deploy/order-demo/app.py deploy/order-demo/http_observability.py ./
ENV APP_COMMIT_SHA=0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38
CMD ["python", "app.py"]

FROM postgres:17.11-alpine AS order-db
COPY deploy/order-demo/init.sql /docker-entrypoint-initdb.d/10-orders.sql
COPY deploy/order-demo/postgres-monitoring.sql /tmp/postgres-monitoring.sql
RUN sed 's/\r$//' /tmp/postgres-monitoring.sql \
    | sed "s/^-- PASSWORD_SET_BY_SETUP$/ALTER ROLE postgres_monitor PASSWORD 'local-exporter-demo-only';/" \
    > /docker-entrypoint-initdb.d/20-monitoring.sql \
    && grep -q '^ALTER ROLE postgres_monitor PASSWORD ' /docker-entrypoint-initdb.d/20-monitoring.sql

FROM prom/prometheus:v3.15.0 AS prometheus
COPY deploy/order-demo/alerts.yml /etc/prometheus/alerts.yml
COPY deploy/prometheus.yml /etc/prometheus/prometheus.yml

FROM grafana/loki:3.7.0 AS loki
COPY deploy/order-demo/loki-config.yaml /etc/loki/demo.yaml
COPY --from=probe /bin/busybox /bin/busybox
USER root
RUN ["/bin/busybox", "install", "-d", "-o", "10001", "-g", "10001", "/tmp/loki"]
USER 10001:10001

FROM grafana/alloy:v1.20.1 AS alloy
COPY deploy/order-demo/alloy-config.alloy /etc/alloy/config.alloy
COPY --from=probe /bin/busybox /bin/busybox
RUN sed -i 's/host\.docker\.internal:3100/loki:3100/g' /etc/alloy/config.alloy

FROM node:24.15.0-bookworm-slim AS frontend-build
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend ./
ENV VITE_API_BASE_URL=/
RUN npm run build

FROM nginx:1.28.3-alpine AS frontend
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /ui/dist /usr/share/nginx/html
