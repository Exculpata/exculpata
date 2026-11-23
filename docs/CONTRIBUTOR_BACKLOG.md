# Contributor starting points

Use the [product north star](PRODUCT_NORTH_STAR.md) to frame the user outcome.
The [assessment and delivery plan](PRODUCT_DIRECTION_2026-09-12.md) connects
individual slices to the complete case-team review experience.

For scoped product work, start with the [portable product slices](product-slices/README.md)
and open GitHub issues and milestones (the older [exit-alpha cruise](EXIT_ALPHA_CRUISE.md)
is retired). Refresh upstream and coordinate remaining work before starting.

These are candidate contributions, not assignments. Open a short issue with a
synthetic reproduction or proposed acceptance path; check existing PRs before
starting. Maintainers coordinate ownership and break larger work into small
reviewable changes. No access to a private deployment is needed.

| Area | Useful first contribution | Acceptance direction |
| --- | --- | --- |
| Browser CI | Maintain the hosted synthetic intake and Report-bundle journeys; extend scoped coverage | Synthetic-only; assertion failures fail CI; bounded startup and cleanup |
| Database CI | Maintain the `postgres-integration` Quality gates job and extend synthetic coverage | Existing database tests must pass without skips; slices 00/08 exact-search PostgreSQL acceptance remains pending |
| Accessibility | Audit one Sources or Reports workflow for keyboard use and reflow | Reproducible before/after browser evidence; no global redesign |
| Installation & model APIs | Exercise fresh endpoint-based installations and operator-managed HTTPS model APIs (`vLLM`, `Ollama`, speech APIs) | Record generic prerequisites, confusing steps, and recovery without real logs |
| Case Assistant evaluation | Expand synthetic multi-document reasoning scenarios for `case_assistant_eval` | Synthetic-only; verifiable math, citations, and honest gap reporting |

Current capabilities and limits are in the README. Authentication, retention,
deletion, import/export formats, shared editing, model changes, and migrations
need an agreed contract before implementation. Ongoing explicit-close matters
are supported (see [matter lifecycle](CASE_LIFECYCLE.md)); reopenable archive
import packages remain a design proposal, not a supported feature.
