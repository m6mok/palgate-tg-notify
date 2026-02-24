from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry
from typing import Optional
import time


__all__ = ['metrics']


class Metrics:
    """Metrics collection for Palgate Telegram Notifier"""

    def __init__(self, registry: Optional[CollectorRegistry] = None):
        self.registry = registry

        # HTTP request metrics
        self.http_requests_total = Counter(
            'palgate_http_requests_total',
            'Total number of HTTP requests made',
            ['method', 'endpoint', 'status'],
            registry=registry
        )

        self.http_request_duration_seconds = Histogram(
            'palgate_http_request_duration_seconds',
            'HTTP request duration in seconds',
            ['method', 'endpoint'],
            registry=registry,
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0)
        )

        # Log processing metrics
        self.log_items_processed_total = Counter(
            'palgate_log_items_processed_total',
            'Total number of log items processed',
            ['type'],
            registry=registry
        )

        self.log_items_sent_total = Counter(
            'palgate_log_items_sent_total',
            'Total number of log items sent to Telegram',
            registry=registry
        )

        self.log_processing_duration_seconds = Histogram(
            'palgate_log_processing_duration_seconds',
            'Log processing duration in seconds',
            registry=registry,
            buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0)
        )

        # Application metrics
        self.application_uptime_seconds = Gauge(
            'palgate_application_uptime_seconds',
            'Application uptime in seconds',
            registry=registry
        )

        self.application_errors_total = Counter(
            'palgate_application_errors_total',
            'Total number of application errors',
            ['error_type'],
            registry=registry
        )

        self.cron_iterations_total = Counter(
            'palgate_cron_iterations_total',
            'Total number of cron iterations',
            registry=registry
        )

        self.cache_operations_total = Counter(
            'palgate_cache_operations_total',
            'Total number of cache operations',
            ['operation', 'status'],
            registry=registry
        )

        # Telegram metrics
        self.telegram_messages_sent_total = Counter(
            'palgate_telegram_messages_sent_total',
            'Total number of Telegram messages sent',
            ['chat_type'],
            registry=registry
        )

        self.telegram_errors_total = Counter(
            'palgate_telegram_errors_total',
            'Total number of Telegram errors',
            registry=registry
        )

        # Start time for uptime calculation
        self.start_time = time.time()

    def update_uptime(self) -> None:
        """Update the application uptime metric"""
        self.application_uptime_seconds.set(time.time() - self.start_time)

    def record_http_request(self, method: str, endpoint: str, status: str, duration: float) -> None:
        """Record HTTP request metrics"""
        self.http_requests_total.labels(method=method, endpoint=endpoint, status=status).inc()
        self.http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)

    def record_log_processing(self, item_type: str, count: int, duration: float, sent_count: int = 0) -> None:
        """Record log processing metrics"""
        self.log_items_processed_total.labels(type=item_type).inc(count)
        self.log_processing_duration_seconds.observe(duration)
        if sent_count > 0:
            self.log_items_sent_total.inc(sent_count)

    def record_error(self, error_type: str) -> None:
        """Record application error"""
        self.application_errors_total.labels(error_type=error_type).inc()

    def record_cron_iteration(self) -> None:
        """Record cron iteration"""
        self.cron_iterations_total.inc()

    def record_cache_operation(self, operation: str, status: str) -> None:
        """Record cache operation"""
        self.cache_operations_total.labels(operation=operation, status=status).inc()

    def record_telegram_message(self, chat_type: str, success: bool = True) -> None:
        """Record Telegram message sent"""
        self.telegram_messages_sent_total.labels(chat_type=chat_type).inc()
        if not success:
            self.telegram_errors_total.inc()


# Global metrics instance
metrics = Metrics()
