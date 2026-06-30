"""
Phase 5.1: API Request Priority Queue
Prioritizes user-requested stocks over background scan stocks.
Uses asyncio.PriorityQueue for efficient request handling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine
from enum import IntEnum
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """Request priority levels (lower number = higher priority)."""
    CRITICAL = 0  # User-facing real-time requests
    HIGH = 1      # User-requested stock details
    NORMAL = 2    # Interactive user actions
    LOW = 3       # Background scans
    BACKGROUND = 4  # Periodic maintenance


@dataclass(order=True)
class PrioritizedRequest:
    """A prioritized request item."""
    priority: int
    timestamp: float = field(compare=False)
    request_id: str = field(compare=False)
    coro: Coroutine = field(compare=False)
    callback: Callable | None = field(default=None, compare=False)
    metadata: dict = field(default_factory=dict, compare=False)


class RequestQueueService:
    """Priority queue for API requests."""
    
    def __init__(self, max_workers: int = 5):
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.max_workers = max_workers
        self.workers: list[asyncio.Task] = []
        self.running = False
        self._request_counter = 0
        logger.info(f"Request Queue Service initialized with {max_workers} workers")

    async def start(self):
        """Start the queue workers."""
        try:
            if self.running:
                logger.warning("Request queue already running")
                return
            
            self.running = True
            self.workers = [
                asyncio.create_task(self._worker(i))
                for i in range(self.max_workers)
            ]
            logger.info(f"Started {len(self.workers)} queue workers")
        
        except Exception as e:
            logger.error(f"Failed to start request queue: {e}", exc_info=True)

    async def stop(self):
        """Stop the queue workers."""
        try:
            self.running = False
            
            # Wait for workers to finish
            await asyncio.gather(*self.workers, return_exceptions=True)
            
            logger.info("Request queue stopped")
        
        except Exception as e:
            logger.error(f"Error stopping request queue: {e}")

    async def enqueue(
        self,
        coro: Coroutine,
        priority: Priority = Priority.NORMAL,
        callback: Callable | None = None,
        metadata: dict | None = None
    ) -> str:
        """
        Add a request to the queue.
        
        Args:
            coro: Coroutine to execute
            priority: Request priority
            callback: Optional callback function
            metadata: Optional metadata dict
        
        Returns:
            Request ID
        """
        try:
            self._request_counter += 1
            request_id = f"req_{self._request_counter}_{int(datetime.now().timestamp() * 1000)}"
            
            request = PrioritizedRequest(
                priority=priority.value,
                timestamp=datetime.now().timestamp(),
                request_id=request_id,
                coro=coro,
                callback=callback,
                metadata=metadata or {}
            )
            
            await self.queue.put(request)
            
            logger.debug(f"Enqueued request {request_id} with priority {priority.name}")
            return request_id
        
        except Exception as e:
            logger.error(f"Failed to enqueue request: {e}", exc_info=True)
            raise

    async def _worker(self, worker_id: int):
        """Worker coroutine that processes requests from the queue."""
        logger.info(f"Worker {worker_id} started")
        
        while self.running:
            try:
                # Get next request (blocks until available)
                try:
                    request = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Execute the request
                try:
                    result = await request.coro
                    
                    # Call callback if provided
                    if request.callback:
                        try:
                            if asyncio.iscoroutinefunction(request.callback):
                                await request.callback(result)
                            else:
                                request.callback(result)
                        except Exception as e:
                            logger.error(f"Callback failed for {request.request_id}: {e}")
                    
                    logger.debug(f"Worker {worker_id} completed request {request.request_id}")
                
                except Exception as e:
                    logger.error(
                        f"Worker {worker_id} failed to execute request {request.request_id}: {e}",
                        exc_info=True
                    )
                
                finally:
                    self.queue.task_done()
            
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}", exc_info=True)
        
        logger.info(f"Worker {worker_id} stopped")

    def get_queue_size(self) -> int:
        """Get current queue size."""
        try:
            return self.queue.qsize()
        except Exception:
            return 0

    def get_stats(self) -> dict[str, Any]:
        """Get queue statistics."""
        try:
            return {
                "running": self.running,
                "workers": len(self.workers),
                "queue_size": self.get_queue_size(),
                "max_workers": self.max_workers
            }
        except Exception as e:
            logger.error(f"Failed to get queue stats: {e}")
            return {"error": str(e)}


# Singleton instance
_queue_instance: RequestQueueService | None = None


def get_request_queue() -> RequestQueueService:
    """Get the singleton request queue instance."""
    global _queue_instance
    if _queue_instance is None:
        _queue_instance = RequestQueueService()
    return _queue_instance


async def enqueue_user_request(coro: Coroutine, **kwargs) -> str:
    """Helper: Enqueue a user request with HIGH priority."""
    queue = get_request_queue()
    return await queue.enqueue(coro, priority=Priority.HIGH, **kwargs)


async def enqueue_background_request(coro: Coroutine, **kwargs) -> str:
    """Helper: Enqueue a background request with LOW priority."""
    queue = get_request_queue()
    return await queue.enqueue(coro, priority=Priority.LOW, **kwargs)
