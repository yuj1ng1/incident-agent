import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from flask import Flask, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from psycopg.rows import dict_row
from http_observability import instrument_business_requests

app = Flask(__name__)

# 这是当前进程初始化时间，不是 CI/CD deployment 时间。
PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
APP_COMMIT_SHA = os.getenv("APP_COMMIT_SHA", "").strip()
# 未提供完整、合法的 Git SHA 时，明确报告 unknown。
if not re.fullmatch(r"[0-9a-fA-F]{40}", APP_COMMIT_SHA):
    APP_COMMIT_SHA = None
else:
    APP_COMMIT_SHA = APP_COMMIT_SHA.lower()

# 启动时读取；默认关闭，修改环境变量后需重启 Flask。
SIMULATE_CODE_REGRESSION = os.getenv(
    "SIMULATE_CODE_REGRESSION", "false"
).strip().lower() in ("true", "1", "yes", "on")
SIMULATE_SLOW_REQUEST = os.getenv(
    "SIMULATE_SLOW_REQUEST", "false"
).strip().lower() in ("true", "1", "yes", "on")

# 日志目录固定在 app.py 所在目录下，日志追加写入 app.log。
log_dir = Path(__file__).resolve().parent / "logs"
log_dir.mkdir(exist_ok=True)

logger = logging.getLogger("order-service")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(file_handler)

# 记录业务接口的访问次数；/health 和 /metrics 不计入业务请求。
request_counter = Counter(
    "demo_http_requests_total",
    "Total number of HTTP requests",
    ["service", "status"]
)
request_histogram = instrument_business_requests(
    app,
    request_counter,
    "order-service",
    paths={"/", "/error", "/order"},
    slow_enabled=lambda: SIMULATE_SLOW_REQUEST,
)

# 首次数据库请求后才有样本；1/0 表示最近一次操作成功/失败。
dependency_up = Gauge(
    "demo_dependency_up",
    "Whether the last database operation succeeded (1) or failed (0)",
    ["service", "dependency"]
)

@app.route("/", methods=["GET"])
def hello():
    logger.info("service=order-service status=200 request completed")

    return Response(
        "Hello Prometheus",
        status=200,
        mimetype="text/plain"
    )

@app.route("/error", methods=["GET"])
def error():
    logger.error("service=order-service status=500 request failed")

    return Response(
        "Internal Server Error",
        status=500,
        mimetype="text/plain"
    )

@app.route("/order", methods=["GET"])
def order():
    # 每次请求建立真实连接，退出 with 后关闭；数据库恢复后无需重启 Flask。
    database_password = os.getenv("ORDER_DB_PASSWORD", "demo")
    database_operation = "connect"
    try:
        with psycopg.connect(
            host=os.getenv("ORDER_DB_HOST", "127.0.0.1"),
            port=os.getenv("ORDER_DB_PORT", "5432"),
            dbname="orders",
            user="demo",
            password=database_password,
            connect_timeout=3,
            options="-c statement_timeout=3000",
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            database_operation = "query"
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, item, quantity FROM orders ORDER BY id LIMIT 1")
                order_row = cursor.fetchone()
    except psycopg.Error as exc:
        # 错误来自实际连接或查询；不让数据库异常终止 Flask。
        error_type = (
            "database_connection_error"
            if database_operation == "connect"
            else "database_query_error"
        )
        error_message = " ".join(str(exc).split())
        if database_password:
            error_message = error_message.replace(database_password, "[REDACTED]")
        dependency_up.labels(service="order-service", dependency="order-db").set(0)
        logger.error(
            "service=order-service dependency=order-db path=/order status=500 "
            "error_type=%s exception_type=%s error_message=%s request failed",
            error_type,
            type(exc).__name__,
            json.dumps(error_message, ensure_ascii=False),
        )
        return {
            "service": "order-service",
            "dependency": "order-db",
            "error_type": error_type,
            "message": "Database operation failed",
        }, 500

    dependency_up.labels(service="order-service", dependency="order-db").set(1)
    # 数据库查询成功且连接已关闭后，模拟后续应用业务逻辑异常。
    try:
        if SIMULATE_CODE_REGRESSION:
            raise RuntimeError("simulated application code regression")
    except RuntimeError as exc:
        logger.error(
            "service=order-service path=/order status=500 "
            "error_type=application_error exception_type=%s error_message=%s request failed",
            type(exc).__name__,
            json.dumps(str(exc), ensure_ascii=False),
        )
        return {
            "service": "order-service",
            "error_type": "application_error",
            "message": str(exc),
        }, 500

    logger.info(
        "service=order-service dependency=order-db path=/order status=200 request completed"
    )
    return {
        "service": "order-service",
        "dependency": "order-db",
        "order": order_row,
    }, 200

@app.route("/metrics", methods=["GET"])
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)

@app.route("/health", methods=["GET"])
def health():
    # 仅检查服务进程可访问；故意不查询 PostgreSQL。
    return {
        "service":"order-service",
        "health":"ok",
        "message":"health endpoint is reachable"
    },200

@app.route("/version", methods=["GET"])
def version():
    # 仅公开显式允许的非敏感字段；不枚举环境变量。
    return {
        "service": "order-service",
        "commit_sha": APP_COMMIT_SHA,
        "short_sha": APP_COMMIT_SHA[:7] if APP_COMMIT_SHA else None,
        "process_started_at": PROCESS_STARTED_AT,
        "runtime_flags": {
            "simulate_code_regression": SIMULATE_CODE_REGRESSION,
            "simulate_slow_request": SIMULATE_SLOW_REQUEST,
        },
    }, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)
