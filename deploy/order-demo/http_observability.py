"""One business request -> one Counter increment and Histogram observation."""
import time

from flask import g, request
from prometheus_client import Histogram


SLOW_REQUEST_SECONDS = 0.8
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)


def instrument_business_requests(app, counter, service, paths, slow_enabled, registry=None):
    options = {"registry": registry} if registry is not None else {}
    histogram = Histogram(
        "demo_http_request_duration_seconds",
        "Business HTTP request duration in seconds",
        ["service", "status"],
        buckets=LATENCY_BUCKETS,
        **options,
    )

    @app.before_request
    def begin_business_request():
        if request.path in paths:
            g.business_started_at = time.perf_counter()
            if slow_enabled():
                time.sleep(SLOW_REQUEST_SECONDS)

    @app.after_request
    def finish_business_request(response):
        started = getattr(g, "business_started_at", None)
        if started is not None:
            status = str(response.status_code)
            counter.labels(service=service, status=status).inc()
            histogram.labels(service=service, status=status).observe(
                time.perf_counter() - started
            )
        return response

    return histogram
