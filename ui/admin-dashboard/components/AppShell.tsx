"use client";

/**
 * AppShell â€” global navigation chrome for the admin dashboard.
 *
 * Renders the sidebar + topbar around every page (wired in
 * ``app/layout.tsx``). The sidebar groups navigation entries by
 * concern (Setup, Operations, Governance) and highlights the active
 * route via Next's ``usePathname``.
 *
 * Visual styling lives entirely in ``app/globals.css``; this file
 * stays declarative â€” a flat list of nav entries plus a small
 * presentational shell.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { getStreamlitUrl } from "@/lib/config";

type NavEntry = {
  href: string;
  label: string;
  icon: string;
  /** Match nested routes (e.g. ``/services/foo``) when true. */
  matchPrefix?: boolean;
  external?: boolean;
};

type NavGroup = {
  label: string;
  items: NavEntry[];
};

const STREAMLIT_URL = getStreamlitUrl();

const NAV_GROUPS: NavGroup[] = [
  {
    label: "Genel",
    items: [
      { href: "/", label: "Kurulum", icon: "ST" },
      { href: "/operations", label: "Operasyonlar", icon: "OP", matchPrefix: true },
    ],
  },
  {
    label: "Yönetim",
    items: [
      { href: "/departments", label: "Departmanlar", icon: "DP", matchPrefix: true },
      { href: "/services", label: "Servisler", icon: "SV", matchPrefix: true },
      { href: "/workflows", label: "İş akışları", icon: "WF", matchPrefix: true },
      { href: "/po-review", label: "PO Review", icon: "PO", matchPrefix: true },
      { href: "/capabilities", label: "Yetenek matrisi", icon: "CP" },
      { href: "/live-smoke", label: "Canlı test", icon: "LS" },
    ],
  },
  {
    label: "Gözlemlenebilirlik",
    items: [
      { href: "/costs", label: "Maliyetler", icon: "CO" },
      { href: "/logs", label: "Loglar", icon: "LG" },
      { href: "/audit", label: "Denetim kaydı", icon: "AL" },
      { href: "/mcp-traffic", label: "MCP trafiği", icon: "MT" },
      { href: "/notifications", label: "Bildirimler", icon: "NT" },
    ],
  },
  {
    label: "Yapılandırma",
    items: [
      { href: "/feature-flags", label: "Özellik bayrakları", icon: "FF" },
      { href: "/llm-providers", label: "AI modelleri", icon: "AI" },
      { href: "/firecrawl", label: "Firecrawl izin listesi", icon: "FC" },
      { href: "/prompts", label: "Promptlar", icon: "PR", matchPrefix: true },
      { href: "/security", label: "Güvenlik", icon: "SC", matchPrefix: true },
    ],
  },
  {
    label: "Hata Ayıklama",
    items: [
      { href: `${STREAMLIT_URL}/explorer`, label: "MCP Explorer", icon: "EX", external: true },
      { href: `${STREAMLIT_URL}/mcp_inspector`, label: "MCP Inspector", icon: "MI", external: true },
    ],
  },
];

const ROUTE_TITLE: Record<string, string> = {
  "/": "Kurulum sihirbazı",
  "/operations": "Operasyonlar",
  "/departments": "Departmanlar",
  "/services": "Servisler",
  "/workflows": "İş akışları",
  "/po-review": "PO Review",
  "/capabilities": "Yetenek matrisi",
  "/live-smoke": "Canlı test",
  "/costs": "Maliyetler",
  "/logs": "Loglar",
  "/audit": "Denetim kaydı",
  "/mcp-traffic": "MCP trafiği",
  "/notifications": "Bildirimler",
  "/feature-flags": "Özellik bayrakları",
  "/llm-providers": "AI modelleri",
  "/firecrawl": "Firecrawl izin listesi",
  "/prompts": "Promptlar",
  "/security": "Güvenlik",
};

function pickTitle(pathname: string): string {
  // Exact match first, then longest prefix (e.g. /services/foo â†’ Servisler).
  if (ROUTE_TITLE[pathname]) return ROUTE_TITLE[pathname];
  const sortedKeys = Object.keys(ROUTE_TITLE).sort((a, b) => b.length - a.length);
  for (const key of sortedKeys) {
    if (key !== "/" && pathname.startsWith(key)) return ROUTE_TITLE[key];
  }
  return "Admin Dashboard";
}

function isActive(pathname: string, entry: NavEntry): boolean {
  if (entry.href === "/") return pathname === "/";
  if (entry.matchPrefix) return pathname === entry.href || pathname.startsWith(entry.href + "/");
  return pathname === entry.href;
}

export default function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const title = pickTitle(pathname);

  return (
    <div className="app-shell">
      <aside className="app-sidebar" aria-label="Birincil gezinme">
        <div className="app-sidebar__brand">
          <div className="app-sidebar__brand-mark" aria-hidden>YA</div>
          <div className="app-sidebar__brand-text">
            <span className="app-sidebar__brand-name">Admin Console</span>
            <span className="app-sidebar__brand-sub">AI Bot Platform</span>
          </div>
        </div>

        <nav>
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="app-sidebar__group">
              <div className="app-sidebar__group-label">{group.label}</div>
              {group.items.map((entry) => {
                const active = isActive(pathname, entry);
                const className = `app-sidebar__link${active ? " is-active" : ""}`;
                const content = (
                  <>
                    <span className="app-sidebar__link-icon" aria-hidden>{entry.icon}</span>
                    <span>{entry.label}</span>
                  </>
                );
                if (entry.external) {
                  return (
                    <a
                      key={entry.href}
                      href={entry.href}
                      className={className}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {content}
                    </a>
                  );
                }
                return (
                  <Link
                    key={entry.href}
                    href={entry.href}
                    className={className}
                    aria-current={active ? "page" : undefined}
                  >
                    {content}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="app-sidebar__footer">
          <div>v0.1.0 - admin-dashboard</div>
        </div>
      </aside>

      <header className="app-topbar">
        <div className="app-topbar__title">
          <span>{title}</span>
        </div>
        <div className="app-topbar__actions">
          <a
            className="btn btn--ghost btn--sm"
            href={STREAMLIT_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            End-User UI
          </a>
          <span className="app-topbar__user">
            <span className="app-topbar__avatar" aria-hidden>AD</span>
            <span>Admin</span>
          </span>
        </div>
      </header>

      <main className="app-main">
        <div className="page-container">{children}</div>
      </main>
    </div>
  );
}
