"use client";

import { useState } from "react";

type Mode = "cloud" | "dc";

type Field = {
  key: string;
  label: string;
  placeholder: string;
  help: string;
  required?: boolean;
};

const cloudFields: Field[] = [
  {
    key: "jira_url",
    label: "JIRA_URL",
    placeholder: "https://your-company.atlassian.net",
    help: "Jira Cloud site URL. User credential is sent with each MCP request.",
    required: true,
  },
  {
    key: "confluence_url",
    label: "CONFLUENCE_URL",
    placeholder: "https://your-company.atlassian.net/wiki",
    help: "Confluence Cloud URL. User credential is sent with each MCP request.",
    required: true,
  },
  {
    key: "bitbucket_url",
    label: "BITBUCKET_URL",
    placeholder: "https://bitbucket.org",
    help: "Bitbucket Cloud base URL. User credential is sent with each MCP request.",
    required: true,
  },
];

const dcFields: Field[] = [
  {
    key: "jira_url",
    label: "JIRA_URL",
    placeholder: "https://jira.your-company.com",
    help: "Jira Server/Data Center URL. User PAT is sent with each MCP request.",
    required: true,
  },
  {
    key: "confluence_url",
    label: "CONFLUENCE_URL",
    placeholder: "https://confluence.your-company.com",
    help: "Confluence Server/Data Center URL. User PAT is sent with each MCP request.",
    required: true,
  },
  {
    key: "bitbucket_url",
    label: "BITBUCKET_URL",
    placeholder: "https://bitbucket.your-company.com",
    help: "Bitbucket Server/Data Center URL. User PAT is sent with each MCP request.",
    required: true,
  },
];

export default function McpDeploymentSelector() {
  const [mode, setMode] = useState<Mode>("cloud");
  const fields = mode === "cloud" ? cloudFields : dcFields;

  return (
    <section className="card">
      <div className="card__header">
        <div>
          <div className="card__title">MCP Baslatma Secimi</div>
          <div className="card__sub">
            MCP sadece deployment tipi ve Atlassian URL'leri ile baslar.
          </div>
        </div>
        <div
          style={{ display: "flex", gap: "0.5rem" }}
          role="tablist"
          aria-label="Deployment tipi"
        >
          <button
            type="button"
            className={mode === "cloud" ? "btn btn--primary btn--sm" : "btn btn--sm"}
            onClick={() => setMode("cloud")}
          >
            Cloud
          </button>
          <button
            type="button"
            className={mode === "dc" ? "btn btn--primary btn--sm" : "btn btn--sm"}
            onClick={() => setMode("dc")}
          >
            Local/DC
          </button>
        </div>
      </div>
      <div className="card__body">
        <div className="grid-2">
          {fields.map((field) => (
            <label key={field.key} className="stack" style={{ gap: "0.35rem" }}>
              <span className="text-sm mono">
                {field.label} ({field.required ? "sart" : "opsiyonel"})
              </span>
              <input
                type="text"
                placeholder={field.placeholder}
                className="input"
                aria-label={field.label}
                required={field.required}
              />
              <span className="text-sm muted">{field.help}</span>
            </label>
          ))}
        </div>
      </div>
    </section>
  );
}
