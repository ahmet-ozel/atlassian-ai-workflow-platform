"use client";

import { useState } from "react";

type Mode = "cloud" | "dc";

type Field = {
  key: string;
  label: string;
  placeholder: string;
  help: string;
  secret?: boolean;
  required?: boolean;
  requirement?: string;
};

const cloudFields: Field[] = [
  {
    key: "jira_url",
    label: "JIRA_URL",
    placeholder: "https://your-company.atlassian.net",
    help: "Jira Cloud site URL.",
    required: true,
  },
  {
    key: "jira_username",
    label: "JIRA_USERNAME",
    placeholder: "your.email@company.com",
    help: "Atlassian hesap e-postasi.",
    required: true,
  },
  {
    key: "jira_api_token",
    label: "JIRA_API_TOKEN",
    placeholder: "ATATT3x...",
    help: "Atlassian API token; Confluence ile aynı token olabilir.",
    secret: true,
    required: true,
  },
  {
    key: "confluence_url",
    label: "CONFLUENCE_URL",
    placeholder: "https://your-company.atlassian.net/wiki",
    help: "Confluence Cloud URL.",
    required: true,
  },
  {
    key: "confluence_username",
    label: "CONFLUENCE_USERNAME",
    placeholder: "your.email@company.com",
    help: "Atlassian hesap e-postası.",
    required: true,
  },
  {
    key: "confluence_api_token",
    label: "CONFLUENCE_API_TOKEN",
    placeholder: "ATATT3x...",
    help: "Atlassian API token; Jira ile aynı token olabilir.",
    secret: true,
    required: true,
  },
  {
    key: "bitbucket_url",
    label: "BITBUCKET_URL",
    placeholder: "https://bitbucket.org",
    help: "Bitbucket Cloud base URL.",
    required: true,
  },
  {
    key: "bitbucket_username",
    label: "BITBUCKET_USERNAME",
    placeholder: "your.email@company.com",
    help: "API token için e-posta; app password için Bitbucket kullanıcı adı.",
    required: true,
  },
  {
    key: "bitbucket_workspace",
    label: "BITBUCKET_WORKSPACE",
    placeholder: "example_workspace",
    help: "Opsiyonel workspace slug. Repo URL'sindeki bitbucket.org/{workspace}/{repo} bölümü.",
  },
  {
    key: "bitbucket_api_token",
    label: "BITBUCKET_API_TOKEN",
    placeholder: "bb_pat_xxxxxxxxxxxx",
    help: "Önerilen Bitbucket scoped API token. BITBUCKET_APP_PASSWORD ile alternatiflidir.",
    secret: true,
    requirement: "biri şart",
  },
  {
    key: "bitbucket_app_password",
    label: "BITBUCKET_APP_PASSWORD",
    placeholder: "app_password",
    help: "Legacy alternatif. BITBUCKET_API_TOKEN veya bu alanlardan biri şarttır.",
    secret: true,
    requirement: "biri şart",
  },
];

const dcFields: Field[] = [
  {
    key: "jira_url",
    label: "JIRA_URL",
    placeholder: "https://jira.your-company.com",
    help: "Jira Server/Data Center URL.",
    required: true,
  },
  {
    key: "jira_personal_token",
    label: "JIRA_PERSONAL_TOKEN",
    placeholder: "jira_pat_xxx",
    help: "Jira profilinden üretilen Personal Access Token.",
    secret: true,
    required: true,
  },
  {
    key: "confluence_url",
    label: "CONFLUENCE_URL",
    placeholder: "https://confluence.your-company.com",
    help: "Confluence Server/Data Center URL.",
    required: true,
  },
  {
    key: "confluence_personal_token",
    label: "CONFLUENCE_PERSONAL_TOKEN",
    placeholder: "conf_pat_xxx",
    help: "Confluence profilinden üretilen Personal Access Token.",
    secret: true,
    required: true,
  },
  {
    key: "bitbucket_url",
    label: "BITBUCKET_URL",
    placeholder: "https://bitbucket.your-company.com",
    help: "Bitbucket Server/Data Center URL.",
    required: true,
  },
  {
    key: "bitbucket_personal_token",
    label: "BITBUCKET_PERSONAL_TOKEN",
    placeholder: "bb_pat_xxx",
    help: "Bitbucket profilinden üretilen Personal Access Token.",
    secret: true,
    required: true,
  },
  {
    key: "bitbucket_project_key",
    label: "BITBUCKET_PROJECT_KEY",
    placeholder: "PROJ",
    help: "Opsiyonel varsayılan project key.",
  },
  {
    key: "bitbucket_ssl_verify",
    label: "BITBUCKET_SSL_VERIFY",
    placeholder: "true",
    help: "Opsiyonel. Self-signed sertifika varsa bilinçli olarak false yapılabilir.",
  },
];

export default function McpDeploymentSelector() {
  const [mode, setMode] = useState<Mode>("cloud");
  const fields = mode === "cloud" ? cloudFields : dcFields;

  return (
    <section className="card">
      <div className="card__header">
        <div>
          <div className="card__title">MCP Başlatma Seçimi</div>
          <div className="card__sub">
            Önce deployment tipini seçin, sonra sadece o tipe ait alanları doldurun.
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
              <span className="text-sm mono">
                {field.label} ({field.requirement ?? (field.required ? "sart" : "opsiyonel")})
              </span>
              <input
                type={field.secret ? "password" : "text"}
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
