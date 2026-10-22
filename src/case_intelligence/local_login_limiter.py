"""Bound local password checks without letting one client fill a global key table."""

from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import ipaddress
import math
from threading import Condition
from typing import Callable, Iterator, Literal


def normalize_client_key(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


class LoginThrottled(Exception):
    def __init__(self, seconds: float, now: datetime):
        self.retry_after_seconds = max(1, math.ceil(seconds))
        minutes = math.ceil(self.retry_after_seconds / 60)
        unit = "minute" if minutes == 1 else "minutes"
        super().__init__("Sign-in is temporarily unavailable. "
                         f"Try again in {minutes} {unit}.")


@dataclass
class _DailyWindow:
    slots: dict[int, int] = field(default_factory=dict)
    pending: int = 0

    def prune(self, now: datetime) -> None:
        for slot in list(self.slots):
            if (slot + 1) * 900 + 86400 <= now.timestamp():
                del self.slots[slot]

    @property
    def count(self) -> int:
        return sum(self.slots.values()) + self.pending

    def retry_seconds(self, now: datetime, limit: int) -> float:
        remaining = self.count
        for slot, count in sorted(self.slots.items()):
            remaining -= count
            if remaining < limit:
                return (slot + 1) * 900 + 86400 - now.timestamp()
        return 86400


@dataclass
class _Window:
    failures: deque[datetime] = field(default_factory=deque)
    pending: int = 0

    def prune(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=15)
        while self.failures and self.failures[0] <= cutoff:
            self.failures.popleft()

    @property
    def count(self) -> int:
        return len(self.failures) + self.pending

    def retry_seconds(self, now: datetime, limit: int) -> float:
        expirations = [value + timedelta(minutes=15) for value in self.failures]
        expirations.extend([now + timedelta(minutes=15)] * self.pending)
        return (sorted(expirations)[self.count - limit] - now).total_seconds()


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
    username_daily_limit = 200

    def __init__(self) -> None:
        self.clients: OrderedDict[str, _Client] = OrderedDict()
        self.usernames: dict[int, _Window] = {}
        self.daily: dict[int, _DailyWindow] = {}
        self._condition = Condition()

    def _reserve(
        self, client_key: str, username_key: str, clock: Callable[[], datetime],
    ) -> tuple[_Client, _Window, _Window, _DailyWindow, int]:
        bucket = int(username_key, 16) % self.username_bucket_count
        with self._condition:
            while True:
                now = clock()
                aggregate = self.usernames.get(bucket, _Window())
                aggregate.prune(now)
                daily = self.daily.get(bucket, _DailyWindow())
                daily.prune(now)
                blockers = [(aggregate, self.username_limit), (daily, self.username_daily_limit)]
                client = self.clients.get(client_key)
                if client is not None:
                    client.prune(now)
                    pair = client.usernames.get(username_key, _Window())
                    blockers.extend([(client.total, self.client_total_limit),
                                     (pair, self.client_username_limit)])
                delays = [window.retry_seconds(now, limit) for window, limit in blockers
                          if window.count >= limit]
                if delays:
                    raise LoginThrottled(max(delays), now)
                if client is not None:
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
            self.daily[bucket] = daily
            for window in (pair, client.total, aggregate, daily):
                window.pending += 1
            return client, pair, aggregate, daily, bucket

    @contextmanager
    def attempt(
        self, client_key: str, username_key: str, clock: Callable[[], datetime],
    ) -> Iterator[_Attempt]:
        client, pair, aggregate, daily, bucket = self._reserve(client_key, username_key, clock)
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
                daily.pending -= 1
                daily.prune(now)
                if result.outcome == "failure":
                    slot = int(now.timestamp() // 900)
                    daily.slots[slot] = daily.slots.get(slot, 0) + 1
                if not daily.count:
                    del self.daily[bucket]
                if result.outcome == "success":
                    pair.failures.clear()
                if not pair.count:
                    del client.usernames[username_key]
                if not client.total.count:
                    del self.clients[client_key]
                if not aggregate.count:
                    del self.usernames[bucket]
                self._condition.notify_all()
