"""Durable progress and immutable views, separate from evidence values."""
from dataclasses import dataclass


class Superseded(RuntimeError):
    """Captured inputs no longer describe the current matter."""


class CapacityPlan(RuntimeError):
    def __init__(self, size):
        self.size = size
        self.args = ('projection needs a larger capacity reservation',)


@dataclass
class Budget:
    maximum: int
    allowance: int
    work: int = 0
    control: int = 0
    touches: int = 0
    in_control: bool = False

    @property
    def remaining(self):
        return self.allowance - self.work

    def charge(self, rows, *, credit=False):
        amount = max(1, rows) if credit else rows
        self.touches += rows
        if self.in_control:
            self.control += amount
            if self.control > self.maximum - self.allowance:
                raise RuntimeError('index control row budget exceeded')
        else:
            self.work += amount
            if self.work > self.allowance:
                raise RuntimeError('index work row budget exceeded')
        if self.touches > self.maximum:
            raise RuntimeError('index total row budget exceeded')


@dataclass
class Job:
    matter_id: str
    store: object
    definition: object
    definition_digest: str
    subscription: str = ''
    serial: int = 0
    cancelled: bool = False
    error: str = ''
    plan: tuple | None = None
    iterator: object = None
    attempts: int = 0
    last_budget: Budget | None = None


@dataclass(frozen=True)
class View:
    matter_id: str
    store: object
    definition: object
    definition_digest: str
    generation: str
    target: tuple
    error: str
    repository: object
    target_reader: object
    state: dict
    complete: bool
    total: int
    summaries: dict

    @property
    def filled(self):
        return frozenset(self.summaries.get('filled', ()))

    @property
    def headers(self):
        return tuple(self.summaries.get('headers', ()))

    @property
    def reports(self):
        from case_intelligence.modules.discovery_review.load_files import Report
        return tuple(Report(**report) for report in self.summaries.get('reports', ()))
