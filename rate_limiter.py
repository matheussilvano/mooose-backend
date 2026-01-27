import time
from collections import deque
from threading import Lock

from fastapi import HTTPException, status

_RATE_LIMIT_BUCKETS = {}
_LOCK = Lock()


def enforce_rate_limit(key: str, limit: int = 5, window_seconds: int = 60) -> None:
    now = time.time()
    with _LOCK:
        bucket = _RATE_LIMIT_BUCKETS.setdefault(key, deque())
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Muitas requisições. Tente novamente em instantes.",
            )
        bucket.append(now)
