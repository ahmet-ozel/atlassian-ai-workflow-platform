"""Inbound webhook receivers (Atlassian Jira/Bitbucket/Confluence).

This package contains the webhook processing pipeline and individual
stage implementations for handling Atlassian webhook events.

Main components:
- WebhookPipeline: Sequential orchestrator (dedup  loop_guard  dispatcher)
- WebhookPayload: Normalized webhook event representation
- StageResult / PipelineResult: Stage and pipeline outcome models
- PipelineStage: Protocol for implementing pipeline stages
- DedupStage / LoopGuardStage / DispatcherStage: Adapter classes that
  bridge the per-component result shapes (defined alongside the
  components themselves in dedup.py / loop_guard.py / dispatcher.py)
  onto the pipeline-level :class:`StageResult` contract.
- build_webhook_pipeline: Production-ready factory that wires the
  three components into a :class:`WebhookPipeline` with all required
  collaborators (db pool, audit logger, vault, temporal client,
  jira commenter, admin notifier).
"""

from .pipeline import (
    DedupStage,
    DispatcherStage,
    LoopGuardStage,
    PipelineResult,
    PipelineStage,
    StageAction,
    StageResult,
    WebhookPayload,
    WebhookPipeline,
    build_webhook_pipeline,
    extract_webhook_payload,
    router as pipeline_router,
)

__all__ = [
    "DedupStage",
    "DispatcherStage",
    "LoopGuardStage",
    "PipelineResult",
    "PipelineStage",
    "StageAction",
    "StageResult",
    "WebhookPayload",
    "WebhookPipeline",
    "build_webhook_pipeline",
    "extract_webhook_payload",
    "pipeline_router",
]
