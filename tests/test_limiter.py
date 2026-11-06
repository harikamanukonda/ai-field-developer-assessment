import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from assessment.limiter import TokenLimiter, RateLimitExceeded


def test_window_tenants_persistence_and_settlement(tmp_path):
    now = [100.0]
    path = tmp_path / "usage.db"
    limiter = TokenLimiter(path, limit=100, clock=lambda: now[0])
    reservation = limiter.reserve("a", 80)
    limiter.reserve("b", 100)
    with pytest.raises(RateLimitExceeded):
        TokenLimiter(path, limit=100, clock=lambda: now[0]).reserve("a", 21)
    limiter.settle(reservation, 10)
    limiter.reserve("a", 90)
    now[0] += 60
    limiter.reserve("a", 100)


def test_parallel_reservations_cannot_overbook(tmp_path):
    limiter = TokenLimiter(tmp_path / "usage.db", limit=100)
    def attempt(_):
        try:
            limiter.reserve("a", 10)
            return True
        except RateLimitExceeded:
            return False
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(attempt, range(40))) == 10
