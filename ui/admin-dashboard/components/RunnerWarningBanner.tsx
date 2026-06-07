"use client";

/**
 * RunnerWarningBanner - SSH Runner Single Point of Failure warning.
 * * Queries `GET /admin/ssh-runners` on page
 * load and displays a yellow warning banner when the number of active runners
 * is less than 2.
 * */

import { useCallback, useEffect, useState } from "react";

import { getAdminApiBaseUrl } from "@/lib/config";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type SshRunnerListResponse = {
  active_runners: number;
  runners: unknown[];
  healthcheck_cron_scheduled: boolean;
};

type FetchState = "idle" | "loading" | "success" | "error";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function RunnerWarningBanner() {
  const [activeRunners, setActiveRunners] = useState<number | null>(null);
  const [fetchState, setFetchState] = useState<FetchState>("idle");

  const fetchRunners = useCallback(async () => {
    setFetchState("loading");
    try {
      const baseUrl = getAdminApiBaseUrl();
      const response = await fetch(`${baseUrl}/admin/ssh-runners`, {
        method: "GET",
        headers: { "Content-Type": "application/json" },
      });

      if (!response.ok) {
        setFetchState("error");
        return;
      }

      const data: SshRunnerListResponse = await response.json();
      setActiveRunners(data.active_runners);
      setFetchState("success");
    } catch {
      setFetchState("error");
    }
  }, []);

  // Query on page load
  useEffect(() => {
    void fetchRunners();
  }, [fetchRunners]);

  // Hide banner when runners >= 2
  if (fetchState !== "success" || activeRunners === null || activeRunners >= 2) {
    return null;
  }

  // Show yellow warning banner when active runners < 2
  return (
    <div
      role="alert"
      aria-live="polite"
      style={bannerStyle}
    >
      <span style={iconStyle} aria-hidden="true"></span>
      <span>
        Tek SSH runner = Single Point of Failure. En az 2 active runner önerilir.
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const bannerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  padding: "0.75rem 1rem",
  background: "#fef3c7",
  color: "#92400e",
  borderRadius: "0.375rem",
  border: "1px solid #fcd34d",
  fontSize: "0.9rem",
  fontWeight: 500,
  marginBottom: "1rem",
};

const iconStyle: React.CSSProperties = {
  flexShrink: 0,
  fontSize: "1.1rem",
};
