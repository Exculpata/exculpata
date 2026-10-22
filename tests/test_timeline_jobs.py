"""Refresh admission, scope and terminal-state regressions."""
import pytest
from case_intelligence.timeline_jobs import TimelineJobs
from case_intelligence.workspace_store import WorkspaceProblem

class Background:
    def __init__(self):
        self.tasks=[]
    def add_task(self, function, *args):
        self.tasks.append((function,args))
    def run(self):
        for function,args in self.tasks:
            function(*args)

def test_bounded_jobs_and_actor_isolation():
    jobs,background=TimelineJobs(),Background()
    jobs.submit(background,'matter-a','actor-a',lambda:None)
    assert jobs.state('matter-a','actor-a')=='running'
    assert jobs.state('matter-a','actor-b')==''
    with pytest.raises(WorkspaceProblem,match='already running'):
        jobs.submit(background,'matter-a','actor-a',lambda:None)
    jobs.submit(background,'matter-b','actor-a',lambda:None)
    with pytest.raises(WorkspaceProblem,match='busy'):
        jobs.submit(background,'matter-c','actor-a',lambda:None)
    background.run()
    assert jobs.state('matter-a','actor-a')=='completed'

def test_failure_does_not_store_exception_content():
    jobs,background=TimelineJobs(),Background()
    def fail():
        raise KeyError('Synthetic source disappeared')
    jobs.submit(background,'matter','actor',fail)
    background.run()
    assert jobs.state('matter','actor')=='failed'
