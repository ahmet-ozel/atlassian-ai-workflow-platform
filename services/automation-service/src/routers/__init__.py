"""``routers/`` package - FastAPI router modules for ``automation-service``.

This package houses HTTP routers that sit between the FastAPI
application factory in :mod:`automation_service.app` and the
service-layer orchestrators under :mod:`services`.  The routers are
intentionally **thin** - every endpoint validates the request body,
dispatches to a collaborator pulled off ``request.app.state.<key>``
and translates orchestrator exceptions into HTTP status codes.

Currently exposes:

* :mod:`routers.dept_credentials` - per-service department credential
  CRUD + probe endpoints. Wraps the
  :class:`services.dept_credential_service.DeptCredentialService`
  orchestrator.
"""

__all__: list[str] = []
