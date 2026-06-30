"""
Phase 5.3: Performance Monitoring Service
Tracks scan times, API response times, and logs slow operations.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Any
from datetime import datetime, timedelta
from collections import defaultdict, deque
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class PerformanceMonitor:
    """Monitor and track performance metrics."""
    
    def __init__(self, slow_threshold_sec: float = 2.0, history_size: int = 1000):
        self.slow_threshold = slow_threshold_sec
        self.history_size = history_size
        
        # Track metrics
        self._scan_times: deque = deque(maxlen=history_size)
        self._api_times: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._slow_operations: deque = deque(maxlen=200)
        self._error_counts: Dict[str, int] = defaultdict(int)
        
        logger.info(f"Performance Monitor initialized (slow threshold: {slow_threshold_sec}s)")

    @contextmanager
    def track_operation(self, operation_name: str, metadata: Dict | None = None):
        """
        Context manager to track operation performance.
        
        Usage:
            with perf_monitor.track_operation("scan_market"):
                # ... perform scan ...
        
        Args:
            operation_name: Name of the operation
            metadata: Optional metadata dict
        """
        start_time = time.time()
        success = True
        error = None
        
        try:
            yield
        except Exception as e:
            success = False
            error = str(e)
            self._error_counts[operation_name] += 1
            raise
        finally:
            elapsed = time.time() - start_time
            
            # Record timing
            self._record_timing(operation_name, elapsed, success, error, metadata)
            
            # Log if slow
            if elapsed > self.slow_threshold:
                self._log_slow_operation(operation_name, elapsed, metadata)

    def _record_timing(
        self,
        operation: str,
        elapsed: float,
        success: bool,
        error: str | None,
        metadata: Dict | None
    ):
        """Record operation timing."""
        try:
            record = {
                "operation": operation,
                "elapsed_sec": round(elapsed, 3),
                "success": success,
                "error": error,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {}
            }
            
            # Store by operation type
            if operation == "scan_market":
                self._scan_times.append(record)
            elif operation.startswith("api_"):
                self._api_times[operation].append(record)
            
        except Exception as e:
            logger.warning(f"Failed to record timing: {e}")

    def _log_slow_operation(self, operation: str, elapsed: float, metadata: Dict | None):
        """Log and record slow operations."""
        try:
            slow_record = {
                "operation": operation,
                "elapsed_sec": round(elapsed, 3),
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {}
            }
            
            self._slow_operations.append(slow_record)
            
            logger.warning(
                f"SLOW OPERATION: {operation} took {elapsed:.2f}s (threshold: {self.slow_threshold}s)"
            )
        
        except Exception as e:
            logger.warning(f"Failed to log slow operation: {e}")

    def track_scan_time(self, elapsed_sec: float, symbol_count: int = 0):
        """
        Track a market scan operation.
        
        Args:
            elapsed_sec: Time taken for scan
            symbol_count: Number of symbols scanned
        """
        try:
            self._scan_times.append({
                "elapsed_sec": elapsed_sec,
                "symbol_count": symbol_count,
                "timestamp": datetime.now().isoformat()
            })
            
            if elapsed_sec > self.slow_threshold:
                logger.warning(f"Slow market scan: {elapsed_sec:.2f}s for {symbol_count} symbols")
        
        except Exception as e:
            logger.warning(f"Failed to track scan time: {e}")

    def track_api_response(self, endpoint: str, elapsed_sec: float, status_code: int = 200):
        """
        Track an API response time.
        
        Args:
            endpoint: API endpoint name
            elapsed_sec: Response time
            status_code: HTTP status code
        """
        try:
            operation = f"api_{endpoint}"
            self._api_times[operation].append({
                "elapsed_sec": elapsed_sec,
                "status_code": status_code,
                "timestamp": datetime.now().isoformat()
            })
            
            if elapsed_sec > self.slow_threshold:
                logger.warning(f"Slow API response: {endpoint} took {elapsed_sec:.2f}s")
        
        except Exception as e:
            logger.warning(f"Failed to track API response: {e}")

    def get_scan_stats(self) -> Dict[str, Any]:
        """Get market scan statistics."""
        try:
            if not self._scan_times:
                return {"status": "no_data"}
            
            times = [r["elapsed_sec"] for r in self._scan_times if "elapsed_sec" in r]
            
            if not times:
                return {"status": "no_data"}
            
            return {
                "count": len(times),
                "avg_time_sec": round(sum(times) / len(times), 2),
                "min_time_sec": round(min(times), 2),
                "max_time_sec": round(max(times), 2),
                "last_scan_time_sec": round(times[-1], 2) if times else 0,
                "slow_scans": sum(1 for t in times if t > self.slow_threshold)
            }
        
        except Exception as e:
            logger.error(f"Failed to get scan stats: {e}")
            return {"status": "error", "error": str(e)}

    def get_api_stats(self, endpoint: str | None = None) -> Dict[str, Any]:
        """
        Get API response statistics.
        
        Args:
            endpoint: Specific endpoint (or None for all)
        
        Returns:
            Stats dict
        """
        try:
            if endpoint:
                operation = f"api_{endpoint}"
                times_data = list(self._api_times.get(operation, []))
            else:
                # Aggregate all endpoints
                times_data = []
                for endpoint_times in self._api_times.values():
                    times_data.extend(endpoint_times)
            
            if not times_data:
                return {"status": "no_data"}
            
            times = [r["elapsed_sec"] for r in times_data if "elapsed_sec" in r]
            
            if not times:
                return {"status": "no_data"}
            
            return {
                "count": len(times),
                "avg_time_sec": round(sum(times) / len(times), 3),
                "min_time_sec": round(min(times), 3),
                "max_time_sec": round(max(times), 3),
                "p95_time_sec": round(sorted(times)[int(len(times) * 0.95)], 3) if len(times) > 1 else 0,
                "slow_requests": sum(1 for t in times if t > self.slow_threshold)
            }
        
        except Exception as e:
            logger.error(f"Failed to get API stats: {e}")
            return {"status": "error", "error": str(e)}

    def get_slow_operations(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get recent slow operations.
        
        Args:
            limit: Max number of records to return
        
        Returns:
            List of slow operation records
        """
        try:
            return list(self._slow_operations)[-limit:]
        except Exception as e:
            logger.error(f"Failed to get slow operations: {e}")
            return []

    def get_error_counts(self) -> Dict[str, int]:
        """Get error counts by operation."""
        try:
            return dict(self._error_counts)
        except Exception:
            return {}

    def get_summary(self) -> Dict[str, Any]:
        """Get overall performance summary."""
        try:
            return {
                "scan_stats": self.get_scan_stats(),
                "api_stats": self.get_api_stats(),
                "slow_operations_count": len(self._slow_operations),
                "error_counts": self.get_error_counts(),
                "slow_threshold_sec": self.slow_threshold
            }
        except Exception as e:
            logger.error(f"Failed to get performance summary: {e}")
            return {"status": "error", "error": str(e)}

    def reset_stats(self):
        """Reset all statistics."""
        try:
            self._scan_times.clear()
            self._api_times.clear()
            self._slow_operations.clear()
            self._error_counts.clear()
            logger.info("Performance stats reset")
        except Exception as e:
            logger.error(f"Failed to reset stats: {e}")


# Singleton instance
_perf_monitor_instance: PerformanceMonitor | None = None


def get_performance_monitor() -> PerformanceMonitor:
    """Get the singleton performance monitor instance."""
    global _perf_monitor_instance
    if _perf_monitor_instance is None:
        _perf_monitor_instance = PerformanceMonitor()
    return _perf_monitor_instance
