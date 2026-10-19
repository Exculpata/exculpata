"""Bound local password checks without letting one client fill a global key table."""

from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Condition
from typing import Callable, Iterator, Literal


@dataclass
class _Window:
    failures: deque[datetime] = field(default_factory=deque)
    pending: int = 0

    def prune(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=15)
        while self.failures and self.failures[0] < cutoff:
            self.failures.popleft()

    @property
    def count(self) -> int:
        return len(self.failures) + self.pending


@dataclass
class _Client:
    total: _Window = field(default_factory=_Window)
    usernames: dict[str, _Window] = field(default_factory=dict)

    def prune(self, now: datetime) -> None:
        self.total.prune(now)
        for key, window in list(self.usernames.items()):
            window.prune(now)
            if not window.count:
                del self.usernames[key]


@dataclass
class _Attempt:
    outcome: Literal["failure", "success"] | None = None


class LocalLoginLimiter:
    """Exact client windows and conservative, fixed-size username buckets.

    Keys are fixed-size keyed digests supplied by IdentityService. A client
    cannot evict itself, and aggregate counts survive eviction of any client.
    """

    client_capacity = 1_000
    username_bucket_count = 10_000
    client_username_limit = 10
    client_total_limit = 50
    username_limit = 100

    def __init__(self) -> None:
        self.clients: OrderedDict[str, _Client] = OrderedDict()
        self.usernames: dict[int, _Window] = {}
        self._condition = Condition()

    def _reserve(
        self, client_key: str, username_key: str, clock: Callable[[], datetime],
    ) -> tuple[_Client, _Window, _Window, int] | None:
        bucket = int(username_key, 16) % self.username_bucket_count
        with self._condition:
            while True:
                now = clock()
                aggregate = self.usernames.get(bucket, _Window())
                aggregate.prune(now)
                if aggregate.count >= self.username_limit:
                    return None
                client = self.clients.get(client_key)
                if client is not None:
                    client.prune(now)
                    pair = client.usernames.get(username_key, _Window())
                    if (client.total.count >= self.client_total_limit
                            or pair.count >= self.client_username_limit):
                        return None
                    break
                if len(self.clients) < self.client_capacity:
                    client = _Client()
                    pair = _Window()
                    break
                victim = next((key for key, value in self.clients.items()
                               if not value.total.pending), None)
                if victim is not None:
                    del self.clients[victim]
                    continue
                self._condition.wait()
            self.clients[client_key] = client
            self.clients.move_to_end(client_key)
            client.usernames[username_key] = pair
            self.usernames[bucket] = aggregate
            for window in (pair, client.total, aggregate):
                window.pending += 1
            return client, pair, aggregate, bucket

    @contextmanager
    def attempt(
        self, client_key: str, username_key: str, clock: Callable[[], datetime],
    ) -> Iterator[_Attempt | None]:
        reservation = self._reserve(client_key, username_key, clock)
        if reservation is None:
            yield None
            return
        client, pair, aggregate, bucket = reservation
        result = _Attempt()
        try:
            yield result
        finally:
            with self._condition:
                now = clock()
                for window in (pair, client.total, aggregate):
                    window.pending -= 1
                    window.prune(now)
                    if result.outcome == "failure":
                        window.failures.append(now)
                if result.outcome == "success":
                    pair.failures.clear()
                if not pair.count:
                    del client.usernames[username_key]
                if not client.total.count:
                    del self.clients[client_key]
                if not aggregate.count:
                    del self.usernames[bucket]
                self._condition.notify_all()
