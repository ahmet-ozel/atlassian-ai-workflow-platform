"""``services/`` package - domain orchestration helpers for automation-service.

Houses the orchestration / service-layer modules that sit between the
HTTP routers (``src/routers``) and the lower-level building blocks in
``automation_service/`` (probe runner, Vault client, staging helpers).

Currently exposes:

* :mod:`services.dept_credential_service` - atomic per-service
  credential CRUD orchestrator for an *existing* department. Reuses
  the staging pattern primitives from
  :mod:`automation_service.staging` and the
  :class:`automation_service.probe.ProbeRunner` rather than
  duplicating their behaviour.
"""

__all__: list[str] = []
