"use client";

import { useState } from "react";

type Mode = "cloud" | "dc";

type Field = {
  key: string;
  label: string;
  placeholder: string;
  help: string;
  secret?: boolean;
};

const cloudFields: Field[] = [
  {
    key: "jira_url",
    label: "JIRA_URL",
    placeholder: "https://your-company.atlassian.net",
    help: "Jira Cloud site URL.",
  },
  {
    key: "jira_username",
    label: "JIRA_USERNAME",
    placeholder: "your.email@company.com",
    help: "Atlassian hesap e-postasi.",
  },
  {
    key: "jira_api_token",
    label: "JIRA_API_TOKEN",
    placeholder: "ATATT3x...",
    help: "Atlassian API token; Confluence ile ayni token olabilir.",
    secret: true,
  },
  {
    key: "confluence_url",
    label: "CONFLUENCE_URL",
    placeholder: "https://your-company.atlassian.net/wiki",
    help: "Confluence Cloud URL.",
  },
  {
    key: "bitbucket_url",
    label: "BITBUCKET_URL",
    placeholder: "https://bitbucket.org",
    help: "Bitbucket Cloud base URL.",
  },
  {
    key: "bitbucket_workspace",
    label: "BITBUCKET_WORKSPACE",
    placeholder: "example_workspace",
    help: "Repo URL'sindeki bitbucket.org/{workspace}/{repo} bolumu.",
  },
  {
    key: "bitbucket_api_token",
    label: "BITBUCKET_API_TOKEN",
    placeholder: "ATATT3x...",
    help: "Bitbucket scoped API token.",
    secret: true,
  },
];

const dcFields: Field[] = [
  {
    key: "jira_url",
    label: "JIRA_URL",
    placeholder: "https://jira.your-company.com",
    help: "Jira Server/Data Center URL.",
  },
  {
    key: "jira_personal_token",
    label: "JIRA_PERSONAL_TOKEN",
    placeholder: "jira_pat_xxx",
    help: "Jira profilinden uretilen Personal Access Token.",
    secret: true,
  },
  {
    key: "confluence_url",
    label: "CONFLUENCE_URL",
    placeholder: "https://confluence.your-company.com",
    help: "Confluence Server/Data Center URL.",
  },
  {
    key: "confluence_personal_token",
    label: "CONFLUENCE_PERSONAL_TOKEN",
    placeholder: "conf_pat_xxx",
    help: "Confluence profilinden uretilen Personal Access Token.",
    secret: true,
  },
  {
    key: "bitbucket_url",
    label: "BITBUCKET_URL",
    placeholder: "https://bitbucket.your-company.com",
    help: "Bitbucket Server/Data Center URL.",
  },
  {
    key: "bitbucket_personal_token",
    label: "BITBUCKET_PERSONAL_TOKEN",
    placeholder: "bb_pat_xxx",
    help: "Bitbucket profilinden uretilen Personal Access Token.",
    secret: true,
  },
  {
    key: "bitbucket_project_key",
    label: "BITBUCKET_PROJECT_KEY",
    placeholder: "PROJ",
    help: "Opsiyonel varsayilan project key.",
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
            Once deployment tipini sec, sonra sadece o tipe ait alanlari doldur.
          </div>
        </div>
        <div style={{ display: "flex", gap: "0.5rem" }} role="tablist" aria-label="Deployment tipi">
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
            Server/DC
          </button>
        </div>
      </div>
      <div className="card__body">
        <div className="grid-2">
          {fields.map((field) => (
            <label key={field.key} className="stack" style={{ gap: "0.35rem" }}>
              <span className="text-sm mono">{field.label}</span>
              <input
                type={field.secret ? "password" : "text"}
                placeholder={field.placeholder}
                className="input"
                aria-label={field.label}
              />
              <span className="text-sm muted">{field.help}</span>
            </label>
          ))}
        </div>
      </div>
    </section>
  );
}
