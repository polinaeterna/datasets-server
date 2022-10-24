import os

from admin.config import AppConfig
from admin.prometheus import Prometheus
from admin.utils import JobType


def test_prometheus(app_config: AppConfig) -> None:
    is_multiprocess = "PROMETHEUS_MULTIPROC_DIR" in os.environ

    prometheus = Prometheus()
    registry = prometheus.getRegistry()
    assert registry is not None

    content = prometheus.getLatestContent()
    print("content:", content)
    lines = content.split("\n")
    metrics = {line.split(" ")[0]: float(line.split(" ")[1]) for line in lines if line and line[0] != "#"}
    if not is_multiprocess:
        name = "process_start_time_seconds"
        assert name in metrics
        assert metrics[name] > 0
    additional_field = ('pid="' + str(os.getpid()) + '",') if is_multiprocess else ""
    for _, job_type in JobType.__members__.items():
        assert "queue_jobs_total{" + additional_field + 'queue="' + job_type.value + '",status="started"}' in metrics
    # still empty
    assert (
        "responses_in_cache_total{" + additional_field + 'path="/splits",http_status="200",error_code=null}'
        not in metrics
    )
    assert (
        "responses_in_cache_total{" + additional_field + 'path="/first-rows",http_status="200",error_code=null}'
        not in metrics
    )