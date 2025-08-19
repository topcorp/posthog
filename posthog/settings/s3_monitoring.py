"""
S3 monitoring and alerting configuration for PostHog

This module defines thresholds and settings for monitoring S3 operations
and triggering alerts on failures.
"""

from posthog.settings.utils import get_from_env
from posthog.utils import str_to_bool

# S3 Error Rate Alert Thresholds (percentage)
S3_ERROR_RATE_WARNING_THRESHOLD = get_from_env("S3_ERROR_RATE_WARNING_THRESHOLD", 5.0, type_cast=float)
S3_ERROR_RATE_CRITICAL_THRESHOLD = get_from_env("S3_ERROR_RATE_CRITICAL_THRESHOLD", 10.0, type_cast=float)

# Time window for error rate calculation (seconds)
S3_ERROR_RATE_WINDOW_SECONDS = get_from_env("S3_ERROR_RATE_WINDOW_SECONDS", 300, type_cast=int)

# Minimum number of requests before triggering error rate alerts
S3_MIN_REQUESTS_FOR_ALERTING = get_from_env("S3_MIN_REQUESTS_FOR_ALERTING", 10, type_cast=int)

# S3 Latency Alert Thresholds (seconds)
S3_LATENCY_WARNING_THRESHOLD = get_from_env("S3_LATENCY_WARNING_THRESHOLD", 10.0, type_cast=float)
S3_LATENCY_CRITICAL_THRESHOLD = get_from_env("S3_LATENCY_CRITICAL_THRESHOLD", 30.0, type_cast=float)

# Enable S3 monitoring and alerting
S3_MONITORING_ENABLED = get_from_env("S3_MONITORING_ENABLED", True, type_cast=str_to_bool)

# PagerDuty integration for critical S3 alerts
S3_PAGERDUTY_ENABLED = get_from_env("S3_PAGERDUTY_ENABLED", False, type_cast=str_to_bool)

# Slack alerts for S3 monitoring
S3_SLACK_ALERTS_ENABLED = get_from_env("S3_SLACK_ALERTS_ENABLED", False, type_cast=str_to_bool)
S3_SLACK_WEBHOOK_URL = get_from_env("S3_SLACK_WEBHOOK_URL", "", type_cast=str)

# Alert rules configuration
S3_ALERT_RULES = {
    "high_error_rate": {
        "enabled": S3_MONITORING_ENABLED,
        "metric": "posthog_s3_errors_total",
        "threshold": S3_ERROR_RATE_WARNING_THRESHOLD,
        "critical_threshold": S3_ERROR_RATE_CRITICAL_THRESHOLD,
        "window": f"{S3_ERROR_RATE_WINDOW_SECONDS}s",
        "min_requests": S3_MIN_REQUESTS_FOR_ALERTING,
        "description": "S3 error rate is above acceptable threshold",
    },
    "high_latency": {
        "enabled": S3_MONITORING_ENABLED,
        "metric": "posthog_s3_write_duration_seconds",
        "threshold": S3_LATENCY_WARNING_THRESHOLD,
        "critical_threshold": S3_LATENCY_CRITICAL_THRESHOLD,
        "window": f"{S3_ERROR_RATE_WINDOW_SECONDS}s",
        "description": "S3 operation latency is above acceptable threshold",
    },
}

# Prometheus alert rules configuration for export
PROMETHEUS_S3_ALERT_RULES = [
    {
        "alert": "S3HighErrorRate",
        "expr": f'(rate(posthog_s3_errors_total[{S3_ERROR_RATE_WINDOW_SECONDS}s]) / rate(posthog_s3_write_requests_total[{S3_ERROR_RATE_WINDOW_SECONDS}s])) * 100 > {S3_ERROR_RATE_WARNING_THRESHOLD}',
        "for": "2m",
        "labels": {
            "severity": "warning",
            "service": "s3_storage"
        },
        "annotations": {
            "summary": "High S3 error rate detected",
            "description": "S3 error rate is {{ $value }}% over the last 5 minutes, above the threshold of {}%".format(S3_ERROR_RATE_WARNING_THRESHOLD),
            "runbook_url": "https://docs.posthog.com/handbook/engineering/runbooks/s3-errors"
        }
    },
    {
        "alert": "S3CriticalErrorRate", 
        "expr": f'(rate(posthog_s3_errors_total[{S3_ERROR_RATE_WINDOW_SECONDS}s]) / rate(posthog_s3_write_requests_total[{S3_ERROR_RATE_WINDOW_SECONDS}s])) * 100 > {S3_ERROR_RATE_CRITICAL_THRESHOLD}',
        "for": "1m",
        "labels": {
            "severity": "critical",
            "service": "s3_storage"
        },
        "annotations": {
            "summary": "Critical S3 error rate detected",
            "description": "S3 error rate is {{ $value }}% over the last 5 minutes, above the critical threshold of {}%".format(S3_ERROR_RATE_CRITICAL_THRESHOLD),
            "runbook_url": "https://docs.posthog.com/handbook/engineering/runbooks/s3-errors"
        }
    },
    {
        "alert": "S3HighLatency",
        "expr": f'histogram_quantile(0.95, rate(posthog_s3_write_duration_seconds_bucket[{S3_ERROR_RATE_WINDOW_SECONDS}s])) > {S3_LATENCY_WARNING_THRESHOLD}',
        "for": "5m",
        "labels": {
            "severity": "warning",
            "service": "s3_storage"
        },
        "annotations": {
            "summary": "High S3 operation latency detected",
            "description": "95th percentile S3 operation latency is {{ $value }}s over the last 5 minutes, above the threshold of {}s".format(S3_LATENCY_WARNING_THRESHOLD),
            "runbook_url": "https://docs.posthog.com/handbook/engineering/runbooks/s3-latency"
        }
    }
]