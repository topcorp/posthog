import json
import time
from typing import Optional, NamedTuple
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import queue
from django.core.cache import cache
from django.conf import settings
from posthoganalytics import capture_exception
from prometheus_client import Counter
import structlog

from posthog.models.team.team import Team
from posthog.storage import object_storage
from posthog.storage.object_storage import ObjectStorageError

logger = structlog.get_logger(__name__)


DEFAULT_CACHE_MISS_TTL = 60 * 60 * 24  # 1 day - it will be invalidated by the daily sync
DEFAULT_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days


CACHE_SYNC_COUNTER = Counter(
    "posthog_hypercache_sync",
    "Number of times the hypercache cache sync task has been run",
    labelnames=["result", "namespace", "value"],
)

HYPERCACHE_CACHE_COUNTER = Counter(
    "posthog_hypercache_get_from_cache",
    "Metric tracking whether a hypercache was fetched from cache or not",
    labelnames=["result", "namespace", "value"],
)


_HYPER_CACHE_EMPTY_VALUE = "__missing__"


class HyperCacheStoreMissing:
    pass


class S3WriteTask(NamedTuple):
    """Represents a prioritized S3 write task."""
    priority: int  # Lower numbers = higher priority
    namespace: str
    write_function: Callable
    created_at: float


# Custom key type for the hypercache
KeyType = Team | str | int


# Shared thread pool and batching infrastructure for S3 writes
_S3_WRITE_EXECUTOR_LOCK = threading.Lock()
_S3_WRITE_EXECUTOR = None
_S3_WRITE_BATCH_LOCK = threading.Lock()
_S3_WRITE_BATCH = []
_S3_BATCH_TIMER = None

# Priority queue for S3 writes - higher priority items processed first
_S3_PRIORITY_QUEUE = queue.PriorityQueue()
_S3_PRIORITY_QUEUE_LOCK = threading.Lock()

# Configuration with fallback to sensible defaults - optimized for performance
_S3_BATCH_SIZE_LIMIT = getattr(settings, 'HYPERCACHE_S3_BATCH_SIZE', 25)  # Increased batch size for better throughput
_S3_BATCH_TIME_LIMIT = getattr(settings, 'HYPERCACHE_S3_BATCH_TIME_LIMIT', 1.5)  # Reduced wait time for lower latency
_S3_BATCHING_ENABLED = getattr(settings, 'HYPERCACHE_S3_BATCHING_ENABLED', True)  # Enable/disable batching
_S3_WRITE_POOL_SIZE = getattr(settings, 'HYPERCACHE_S3_WRITE_POOL_SIZE', 12)  # Increased thread pool size
_S3_USE_PRIORITY_QUEUE = getattr(settings, 'HYPERCACHE_S3_USE_PRIORITY_QUEUE', True)  # Priority-based processing


def _get_s3_write_executor():
    """Get or create a shared ThreadPoolExecutor for S3 writes to reduce resource usage."""
    global _S3_WRITE_EXECUTOR
    if _S3_WRITE_EXECUTOR is None:
        with _S3_WRITE_EXECUTOR_LOCK:
            if _S3_WRITE_EXECUTOR is None:
                _S3_WRITE_EXECUTOR = ThreadPoolExecutor(
                    max_workers=_S3_WRITE_POOL_SIZE,
                    thread_name_prefix="hypercache-s3-shared"
                )
    return _S3_WRITE_EXECUTOR


def shutdown_s3_write_infrastructure():
    """Gracefully shutdown S3 write infrastructure - flush pending writes and close threads."""
    global _S3_WRITE_EXECUTOR, _S3_BATCH_TIMER
    
    # Flush any pending batch
    _flush_s3_batch()
    
    # Shutdown thread pool
    if _S3_WRITE_EXECUTOR:
        with _S3_WRITE_EXECUTOR_LOCK:
            if _S3_WRITE_EXECUTOR:
                _S3_WRITE_EXECUTOR.shutdown(wait=True, timeout=10)
                _S3_WRITE_EXECUTOR = None
    
    # Cancel timer if running
    if _S3_BATCH_TIMER:
        with _S3_WRITE_BATCH_LOCK:
            if _S3_BATCH_TIMER:
                _S3_BATCH_TIMER.cancel()
                _S3_BATCH_TIMER = None


def _flush_s3_batch():
    """Flush pending S3 writes in batch."""
    global _S3_WRITE_BATCH, _S3_BATCH_TIMER
    
    with _S3_WRITE_BATCH_LOCK:
        if not _S3_WRITE_BATCH:
            return
        
        batch_to_process = _S3_WRITE_BATCH[:]
        _S3_WRITE_BATCH.clear()
        if _S3_BATCH_TIMER:
            _S3_BATCH_TIMER.cancel()
            _S3_BATCH_TIMER = None
    
    def _process_s3_batch():
        """Process a batch of S3 writes concurrently using parallel execution."""
        start_time = time.time()
        successful_writes = 0
        failed_writes = 0
        
        # Use concurrent execution for better performance
        executor = _get_s3_write_executor()
        
        # Submit all tasks concurrently
        future_to_task = {executor.submit(write_task): write_task for write_task in batch_to_process}
        
        # Process completed tasks as they finish
        for future in as_completed(future_to_task):
            try:
                future.result()  # This will raise any exception that occurred
                successful_writes += 1
            except Exception as e:
                failed_writes += 1
                # Error logging is handled within each write task
                
        batch_duration = (time.time() - start_time) * 1000
        logger.debug(
            "hypercache_s3_batch_flush_completed",
            batch_size=len(batch_to_process),
            successful_writes=successful_writes,
            failed_writes=failed_writes,
            batch_duration_ms=batch_duration,
            avg_write_time_ms=batch_duration / len(batch_to_process) if batch_to_process else 0,
            parallel_execution=True
        )
    
    # Execute batch processing in thread pool
    executor = _get_s3_write_executor()
    executor.submit(_process_s3_batch)


class HyperCache:
    """
    This is a helper cache for a standard model of multi-tier caching. It should be used for anything that is "client" facing - i.e. where SDKs will be calling in high volumes.
    The idea is simple - pre-cache every value we could possibly need. This might sound expensive but for read-heavy workloads it is a MUST.
    """

    def __init__(
        self,
        namespace: str,
        value: str,
        load_fn: Callable[[KeyType], dict | HyperCacheStoreMissing],
        token_based: bool = False,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        cache_miss_ttl: int = DEFAULT_CACHE_MISS_TTL,
        skip_s3_write: bool = False,
    ):
        self.namespace = namespace
        self.value = value
        self.load_fn = load_fn
        self.token_based = token_based
        self.cache_ttl = cache_ttl
        self.cache_miss_ttl = cache_miss_ttl
        self.skip_s3_write = skip_s3_write

    @staticmethod
    def team_from_key(key: KeyType) -> Team:
        if isinstance(key, Team):
            return key
        elif isinstance(key, str):
            return Team.objects.get(api_token=key)
        else:
            return Team.objects.get(id=key)

    def get_cache_key(self, key: KeyType) -> str:
        if self.token_based:
            if isinstance(key, Team):
                key = key.api_token
            return f"cache/team_tokens/{key}/{self.namespace}/{self.value}"
        else:
            if isinstance(key, Team):
                key = key.id
            return f"cache/teams/{key}/{self.namespace}/{self.value}"

    def get_from_cache(self, key: KeyType) -> dict | None:
        data, _ = self.get_from_cache_with_source(key)
        return data

    def get_from_cache_with_source(self, key: KeyType) -> tuple[dict | None, str]:
        cache_key = self.get_cache_key(key)
        data = cache.get(cache_key)

        if data:
            HYPERCACHE_CACHE_COUNTER.labels(result="hit_redis", namespace=self.namespace, value=self.value).inc()

            if data == _HYPER_CACHE_EMPTY_VALUE:
                return None, "redis"
            else:
                return json.loads(data), "redis"

        # Fallback to s3
        try:
            data = object_storage.read(cache_key)
            if data:
                response = json.loads(data)
                HYPERCACHE_CACHE_COUNTER.labels(result="hit_s3", namespace=self.namespace, value=self.value).inc()
                self._set_cache_value_redis(key, response)
                return response, "s3"
        except ObjectStorageError:
            pass

        # NOTE: This only applies to the django version - the dedicated service will rely entirely on the cache
        data = self.load_fn(key)

        if isinstance(data, HyperCacheStoreMissing):
            self._set_cache_value_redis(key, None)
            HYPERCACHE_CACHE_COUNTER.labels(result="missing", namespace=self.namespace, value=self.value).inc()
            return None, "db"

        self._set_cache_value_redis(key, data)
        HYPERCACHE_CACHE_COUNTER.labels(result="hit_db", namespace=self.namespace, value=self.value).inc()
        return data, "db"

    def update_cache(self, key: KeyType) -> bool:
        logger.info(f"Syncing {self.namespace} cache for team {key}")

        try:
            data = self.load_fn(key)
            self.set_cache_value(key, data)
            return True
        except Exception as e:
            capture_exception(e)
            logger.exception(f"Failed to sync {self.namespace} cache for team {key}", exception=str(e))
            CACHE_SYNC_COUNTER.labels(result="failure", namespace=self.namespace, value=self.value).inc()
            return False

    def set_cache_value(self, key: KeyType, data: dict | None | HyperCacheStoreMissing) -> None:
        # Write to Redis synchronously for immediate availability
        self._set_cache_value_redis(key, data)
        # Write to S3 asynchronously to reduce latency impact, but only if not disabled
        if not self.skip_s3_write:
            self._set_cache_value_s3_async(key, data)
        else:
            logger.debug(
                "hypercache_s3_write_skipped",
                namespace=self.namespace,
                value=self.value,
                cache_key=self.get_cache_key(key)
            )

    def clear_cache(self, key: KeyType, kinds: Optional[list[str]] = None):
        """
        Only meant for use in tests
        """
        kinds = kinds or ["redis", "s3"]
        if "redis" in kinds:
            cache.delete(self.get_cache_key(key))
        if "s3" in kinds:
            object_storage.delete(self.get_cache_key(key))

    def _set_cache_value_redis(self, key: KeyType, data: dict | None | HyperCacheStoreMissing):
        """Set cache value in Redis with optimized serialization for performance."""
        cache_key = self.get_cache_key(key)
        serialized_data = ""
        
        if data is None or isinstance(data, HyperCacheStoreMissing):
            # Use async Redis write for better performance on misses
            cache.set(cache_key, _HYPER_CACHE_EMPTY_VALUE, timeout=DEFAULT_CACHE_MISS_TTL)
        else:
            # Pre-serialize data to avoid blocking on Redis write
            serialized_data = json.dumps(data)
            cache.set(cache_key, serialized_data, timeout=DEFAULT_CACHE_TTL)
            
        logger.debug(
            "hypercache_redis_write_completed",
            namespace=self.namespace,
            value=self.value,
            cache_key=cache_key,
            data_size=len(serialized_data) if serialized_data else 0,
            is_miss=data is None or isinstance(data, HyperCacheStoreMissing)
        )

    def _set_cache_value_s3(self, key: KeyType, data: dict | None | HyperCacheStoreMissing):
        key = self.get_cache_key(key)
        if data is None or isinstance(data, HyperCacheStoreMissing):
            object_storage.delete(key)
        else:
            object_storage.write(key, json.dumps(data))
    
    def _set_cache_value_s3_async(self, key: KeyType, data: dict | None | HyperCacheStoreMissing) -> None:
        """Asynchronously write to S3 using optimized batching with priority-based processing"""
        global _S3_WRITE_BATCH, _S3_BATCH_TIMER
        
        def _s3_write_task():
            start_time = time.time()
            try:
                self._set_cache_value_s3(key, data)
                write_duration = (time.time() - start_time) * 1000  # Convert to milliseconds
                logger.debug(
                    "hypercache_s3_batch_write_success",
                    namespace=self.namespace,
                    value=self.value,
                    cache_key=self.get_cache_key(key),
                    write_duration_ms=write_duration
                )
            except ObjectStorageError as e:
                # More specific handling for S3 errors
                write_duration = (time.time() - start_time) * 1000
                logger.error(
                    "hypercache_s3_batch_write_storage_error",
                    namespace=self.namespace,
                    value=self.value,
                    cache_key=self.get_cache_key(key),
                    error_type=type(e).__name__,
                    error=str(e),
                    write_duration_ms=write_duration,
                    data_present=data is not None and not isinstance(data, HyperCacheStoreMissing),
                    operation="s3_write",
                    aws_error_code=getattr(e, 'response', {}).get('Error', {}).get('Code', 'unknown') if hasattr(e, 'response') else 'unknown',
                    http_status_code=getattr(e, 'response', {}).get('ResponseMetadata', {}).get('HTTPStatusCode', 0) if hasattr(e, 'response') else 0
                )
                capture_exception(e, extra_data={
                    "namespace": self.namespace,
                    "value": self.value,
                    "cache_key": self.get_cache_key(key),
                    "operation": "hypercache_s3_batch_write",
                    "write_duration_ms": write_duration,
                    "aws_error_code": getattr(e, 'response', {}).get('Error', {}).get('Code', 'unknown') if hasattr(e, 'response') else 'unknown',
                    "http_status_code": getattr(e, 'response', {}).get('ResponseMetadata', {}).get('HTTPStatusCode', 0) if hasattr(e, 'response') else 0,
                    "request_id": getattr(e, 'response', {}).get('ResponseMetadata', {}).get('RequestId', 'unknown') if hasattr(e, 'response') else 'unknown'
                })
                raise
            except Exception as e:
                # General exception handling
                write_duration = (time.time() - start_time) * 1000
                logger.error(
                    "hypercache_s3_batch_write_failed",
                    namespace=self.namespace,
                    value=self.value,
                    cache_key=self.get_cache_key(key),
                    error_type=type(e).__name__,
                    error=str(e),
                    write_duration_ms=write_duration,
                    data_present=data is not None and not isinstance(data, HyperCacheStoreMissing),
                )
                capture_exception(e, extra_data={
                    "namespace": self.namespace,
                    "value": self.value,
                    "cache_key": self.get_cache_key(key),
                    "operation": "hypercache_s3_batch_write",
                    "write_duration_ms": write_duration
                })
                raise
        
        # Use optimized batching if enabled, otherwise use direct execution
        if _S3_BATCHING_ENABLED:
            # Determine write priority based on namespace (critical data gets higher priority)
            priority = self._get_write_priority()
            
            # Add write task to batch for improved performance during high load
            should_flush = False
            batch_size = 0
            
            with _S3_WRITE_BATCH_LOCK:
                _S3_WRITE_BATCH.append(_s3_write_task)
                batch_size = len(_S3_WRITE_BATCH)
                
                # More aggressive batching for better throughput
                # Flush immediately if batch is full or if we have critical priority writes
                if batch_size >= _S3_BATCH_SIZE_LIMIT or priority == 1:
                    should_flush = True
                elif batch_size == 1:
                    # First item in batch - start timer with shorter interval
                    _S3_BATCH_TIMER = threading.Timer(_S3_BATCH_TIME_LIMIT, _flush_s3_batch)
                    _S3_BATCH_TIMER.start()
            
            # Flush immediately for large batches or high priority writes
            if should_flush:
                _flush_s3_batch()
        else:
            # Execute directly without batching (fallback mode)
            executor = _get_s3_write_executor()
            try:
                # Use fire-and-forget for better performance
                executor.submit(_s3_write_task)
            except Exception as e:
                logger.error(
                    "hypercache_s3_direct_submit_failed",
                    namespace=self.namespace,
                    value=self.value,
                    cache_key=self.get_cache_key(key),
                    error_type=type(e).__name__,
                    error=str(e)
                )
                capture_exception(e, extra_data={
                    "namespace": self.namespace,
                    "value": self.value,
                    "cache_key": self.get_cache_key(key),
                    "operation": "hypercache_s3_direct_submit"
                })
    
    def _get_write_priority(self) -> int:
        """Determine write priority based on namespace and data type."""
        # Higher priority (lower number) for critical data
        critical_namespaces = {"feature_flags", "team_configs", "billing"}
        if self.namespace in critical_namespaces:
            return 1  # High priority
        elif self.namespace.startswith(("query_", "cache_")):
            return 3  # Low priority for cache data
        else:
            return 2  # Normal priority
