"""Connect durable compilation jobs to current matter work and atomic Reports."""
from __future__ import annotations

import os

from .generation_matter import generation_matter_scope
from .report_compilation import CompilationBudget, CompilationProblem, compilation_fingerprint, compile_report
from .report_compilation_jobs import ReportCompilationCoordinator, ReportCompilationJobs
from .report_materials import snapshot_report_materials, validate_compiled_material_citations
from .workspace_store import WorkspaceProblem
from .work_product_exports import ExportProblem


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


class ReportCompilationFlow:
    def __init__(self, bench, *, full_text_support_resolver=None):
        self.bench = bench
        self.full_text_support_resolver = full_text_support_resolver
        self.jobs = ReportCompilationJobs(bench.workspace)
        self.budget = CompilationBudget(
            max_materials=_positive_setting("CASE_INTELLIGENCE_REPORT_MAX_MATERIALS", 200),
            max_model_calls=_positive_setting("CASE_INTELLIGENCE_REPORT_MAX_MODEL_CALLS", 12),
            max_sections=_positive_setting("CASE_INTELLIGENCE_REPORT_MAX_SECTIONS", 200),
        )
        self.coordinator = ReportCompilationCoordinator(self.jobs, process=self.process,
            finish=self.finish, workers=_positive_setting("CASE_INTELLIGENCE_REPORT_WORKERS", 1))

    def notify(self):
        self.coordinator.notify()

    def close(self):
        self.coordinator.close()

    def _snapshot(self, job):
        matter = self.bench._matter_by_id(job.matter_id)
        if self.bench.workspace.matter_lifecycle(matter.matter_id).state != "active":
            raise CompilationProblem("This matter is no longer open for review.")
        materials = snapshot_report_materials(self.bench, matter, job.actor_id, job.selections,
            full_text_support_resolver=self.full_text_support_resolver)
        return matter, materials

    def process(self, job, cancelled):
        matter = self.bench._matter_by_id(job.matter_id)
        try:
            with self.bench.source_store(matter).mutation_guard(), self.bench.workspace._lock:
                _, materials = self._snapshot(job)
            fingerprint = compilation_fingerprint(job.kind, job.topic, materials, budget=self.budget, selections=job.selections)
            self.jobs.record_input_fingerprint(job, fingerprint)
            with generation_matter_scope(self.bench.workspace, job.matter_id):
                return compile_report(job.kind, job.topic, materials, self.bench.generator,
                                      budget=self.budget, selections=job.selections, cancelled=cancelled)
        except (WorkspaceProblem, ExportProblem) as exc:
            raise CompilationProblem(str(exc)) from exc
        except KeyError as exc:
            raise CompilationProblem("Some selected work is no longer available. Return to Reports and choose current work.") from exc

    def finish(self, job, draft):
        matter = self.bench._matter_by_id(job.matter_id)
        try:
            with self.bench.source_store(matter).mutation_guard(), self.bench.workspace._lock:
                _, current = self._snapshot(job)
                if compilation_fingerprint(job.kind, job.topic, current, budget=self.budget, selections=job.selections) != draft.fingerprint:
                    raise CompilationProblem("The selected work changed while the report was being compiled. Retry to use the latest review.")
                validate_compiled_material_citations(current, draft.sections)
                return self.jobs.complete(job, lambda: self.bench.workspace.create_report_from_sections(
                    matter.matter_id, job.actor_id, draft.title, draft.purpose,
                    origin_id=job.job_id, sections=draft.sections, transaction_owned=True,
                ), fingerprint=draft.fingerprint)
        except (WorkspaceProblem, ExportProblem) as exc:
            raise CompilationProblem(str(exc)) from exc
        except KeyError as exc:
            raise CompilationProblem("Access or selected work changed before this report could be saved.") from exc
