"use client";

/**
 * Typed wrapper over `apiFetch` for the `/admin/llm-providers` surface.
 * * Each method returns the parsed JSON body (or `void` for the delete
 * path). Errors surface as thrown `ApiError` instances carrying the
 * upstream status code and response body so the calling component can
 * render a targeted toast (e.g. the `provider_in_use` 409 path needs
 * the `dept_ids` array to render the delete-conflict toast).
 */

import { apiFetch } from "@/lib/api-client";
import type {
  ConnectionTestResult,
  DeptOverride,
  ProviderCreatePayload,
  ProviderRow,
  ProviderUpdatePayload,
} from "./types";

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function call<T>(
  path: string,
  init?: RequestInit,
  options?: { allowEmpty?: boolean },
): Promise<T> {
  const res = await apiFetch(path, init);
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    throw new ApiError(res.status, body);
  }
  if (options?.allowEmpty && res.status === 204) {
    return undefined as unknown as T;
  }
  return (await res.json()) as T;
}

export const providerApi = {
  list(): Promise<ProviderRow[]> {
    return call<ProviderRow[]>("/admin/llm-providers");
  },

  get(id: string): Promise<ProviderRow> {
    return call<ProviderRow>(`/admin/llm-providers/${id}`);
  },

  create(payload: ProviderCreatePayload): Promise<ProviderRow> {
    return call<ProviderRow>("/admin/llm-providers", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  update(
    id: string,
    payload: ProviderUpdatePayload,
  ): Promise<ProviderRow> {
    return call<ProviderRow>(`/admin/llm-providers/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  disable(id: string): Promise<ProviderRow> {
    return call<ProviderRow>(`/admin/llm-providers/${id}`, {
      method: "PUT",
      body: JSON.stringify({ status: "inactive" }),
    });
  },

  delete(id: string): Promise<void> {
    return call<void>(
      `/admin/llm-providers/${id}`,
      { method: "DELETE" },
      { allowEmpty: true },
    );
  },

  testSaved(id: string): Promise<ConnectionTestResult> {
    return call<ConnectionTestResult>(
      `/admin/llm-providers/${id}/test`,
      {
        method: "POST",
        body: JSON.stringify({}),
      },
    );
  },

  testUnsaved(
    payload: ProviderCreatePayload,
  ): Promise<ConnectionTestResult> {
    return call<ConnectionTestResult>("/admin/llm-providers/test", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getOverride(deptId: string): Promise<DeptOverride> {
    return call<DeptOverride>(
      `/admin/departments/${encodeURIComponent(deptId)}/llm-provider`,
    );
  },

  setOverride(
    deptId: string,
    providerId: string | null,
  ): Promise<DeptOverride> {
    return call<DeptOverride>(
      `/admin/departments/${encodeURIComponent(deptId)}/llm-provider`,
      {
        method: "PUT",
        body: JSON.stringify({ provider_id: providerId }),
      },
    );
  },
};

export function useProviderApi() {
  return providerApi;
}
