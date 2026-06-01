🚨 AuditPruneWorkflow başarısız

Günlük audit retention cron'u (03:00 UTC) hata verdi. `audit_events` arşivleme veya silme adımı başarısız oldu — operatör müdahalesi gerekli.

**Hata:**
```
{error}
```

**Atılacak adımlar:**
1. `automation-worker` Temporal workflow geçmişini incele (`audit-prune-cron`).
2. MinIO `audit-archive` bucket erişimini ve Postgres bağlantısını doğrula.
3. Düzeltme sonrası workflow'u manuel tetikle.

Runbook: `platform/docs/runbooks/audit_prune_recovery.md`
