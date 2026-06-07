"""Operational scripts for the automation-service.

Currently houses :mod:`src.scripts.probe_atlassian` - the connectivity
probe command referenced from
``platform/config/services.manifest.json`` as the
``connectivity_probe_command`` for the ``automation-service`` entry
(uyumluluk R9, Q10).

The package is import-safe: importing ``src.scripts`` performs no
I/O; only running a sub-module under ``python -m src.scripts.<name>``
performs real work.
"""

__all__: list[str] = []
