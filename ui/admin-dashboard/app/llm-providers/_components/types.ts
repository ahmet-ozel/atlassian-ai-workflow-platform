/**
 * Wire-shape TypeScript types mirroring the backend Pydantic schemas in
 * `platform/services/admin-dashboard-api/src/llm_providers/schemas.py`.
 *
 * Kept in lockstep with the schema module — every field on the
 * backend DTO has a matching property here. The masked-credential
 * fields (`api_key_masked`, `org_id_masked`) are the ONLY credential
 * material the UI ever sees; the raw `api_key` field exists only on
 * the create/update request shapes.
 */

export type ProviderType = "vllm" | "openai" | "anthropic" | "gemini";

export type ProviderStatus = "active" | "inactive";

export interface ProviderRow {
  id: string;
  provider_type: ProviderType;
  name: string;
  model: string;
  context_length: number;
  base_url: string | null;
  status: ProviderStatus;
  api_key_masked: string;
  org_id_masked: string | null;
  last_tested_at: string | null;
  last_test_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProviderCreatePayload {
  provider_type: ProviderType;
  name: string;
  model: string;
  context_length: number;
  base_url?: string;
  api_key?: string;
  org_id?: string;
}

export interface ProviderUpdatePayload {
  name?: string;
  model?: string;
  context_length?: number;
  base_url?: string;
  api_key?: string;
  org_id?: string;
  status?: ProviderStatus;
}

export interface ConnectionTestError {
  status_code: number | null;
  message: string;
}

export interface ConnectionTestResult {
  success: boolean;
  latency_ms: number;
  model: string | null;
  error: ConnectionTestError | null;
}

export interface DeptOverride {
  dept_id: string;
  provider: ProviderRow | null;
}
