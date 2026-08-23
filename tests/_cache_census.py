"""The single-source #652 census of handler-module per-CR cache classification.

Before #652 the #497 census was hand-maintained in TWO places —
``tests/conftest.py`` (``_PER_CR_CACHE_MODULES``) and
``tests/test_cache_keying_convention.py`` (``CACHE_BEARING_MODULES``) —
plus the prose census in ``openstudio_operator/_cr_cache.py``. Neither
tuple was derived from the other, and a handler module that was never
enumerated (``dry_run_audit`` / ``redis_layout_check`` at the time the
fence landed) sailed through CI unclassified. This module is now the
ONE machine-readable census: conftest's autouse reset imports
``CACHE_BEARING_MODULES`` from here, the convention tests import both
tuples from here, and the AST fence in
``test_cache_keying_convention.py`` fails any handler module under
``src/openstudio_operator/handlers/`` that is in NEITHER tuple — the
same derive-the-expection pattern as
``test_python_registry_includes_all_oscm_spawning_handlers`` (#250).

Classification rules:

* ``CACHE_BEARING_MODULES`` — modules holding per-CR caches routed
  through the executable #497/#583 convention
  (:class:`openstudio_operator._cr_cache.PerCRCache` + the uniform
  ``reset_per_cr_caches`` seam). The AST fence also enforces this
  direction: a module-level ``PerCRCache`` instantiation or a
  cache-shaped ``_get_*`` factory MUST land in this tuple.
* ``CACHE_FREE_MODULES`` — modules with NO #497 seam to maintain
  (memory lives in the CR ``.status`` subresource, D04 — or, for
  ``dry_run_audit``, in a dedup cache deliberately outside the
  convention; see ``NON_CONVENTION_CACHE_SHAPED_STATE`` below).
"""

from openstudio_operator.handlers import (
    analysis_sla,
    datapoint_watchdog,
    dry_run_audit,
    redis_layout_check,
    web_background_monitor,
    worker_recycler,
)

#: The #497 census of cache-bearing handler modules — every module listed
#: here MUST expose the uniform ``reset_per_cr_caches`` seam. This tuple
#: is the fence's single source: adding a new per-CR cache means adding
#: the module here (and a seam there) in the same change, so the
#: convention cannot silently regress.
CACHE_BEARING_MODULES = (datapoint_watchdog, web_background_monitor)

#: The #497 census of cache-FREE handler modules — they deliberately
#: expose NO ``reset_per_cr_caches`` seam. ``analysis_sla`` /
#: ``worker_recycler`` keep cross-tick memory in the CR ``.status``
#: subresource (D04); ``redis_layout_check`` re-stamps its gauge per
#: tick; ``dry_run_audit`` holds one deliberately non-convention dedup
#: cache (see ``NON_CONVENTION_CACHE_SHAPED_STATE``). Asserting the
#: seam's absence keeps the census honest in both directions.
CACHE_FREE_MODULES = (analysis_sla, dry_run_audit, redis_layout_check, worker_recycler)

#: Cache-SHAPED module globals deliberately OUTSIDE the #497 convention,
#: keyed by module — the triaged-exceptions list the AST fence honors
#: (same pattern as ``.pip-audit-ignore.txt``: one entry, a rationale,
#: and a fence that fails everything else). Every entry needs a
#: documented lifecycle of its own:
#:
#: * ``dry_run_audit._last_dry_run`` — last-seen ``spec.dryRun`` dedup
#:   map keyed ``(namespace, name)``. Not routed through
#:   :class:`~openstudio_operator._cr_cache.PerCRCache` on purpose: a
#:   cache miss is a harmless BASELINE observation (no Event), entries
#:   are dropped on the DELETED watch event so the #364 delete+recreate
#:   path is handled by the watch stream rather than uid validation,
#:   and ``reset_audit_state()`` is its test seam. See the module
#:   docstring's D04 note.
NON_CONVENTION_CACHE_SHAPED_STATE = {
    dry_run_audit: ("_last_dry_run",),
}
