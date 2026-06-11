"use client";

import type { ReactNode } from "react";

import McpDeploymentSelector from "./McpDeploymentSelector";

const compatibilityRows = [
  { product: "Jira", deployment: "Cloud", support: "Supported" },
  { product: "Jira", deployment: "Local/DC", support: "Server/Data Center" },
  { product: "Confluence", deployment: "Cloud", support: "Supported" },
  { product: "Confluence", deployment: "Local/DC", support: "Server/Data Center" },
  { product: "Bitbucket", deployment: "Cloud", support: "Supported" },
  { product: "Bitbucket", deployment: "Local/DC", support: "Server/Data Center" },
];

const runtimeRows = [
  {
    name: "TRANSPORT",
    value: "streamable-http",
    description: "MCP HTTP transport.",
  },
  {
    name: "STATELESS",
    value: "true",
    description: "Credentials are sent per request; the MCP server keeps no user session.",
  },
  {
    name: "ATLASSIAN_DEPLOYMENT",
    value: "cloud | server",
    description: "Controls Cloud or Local/DC credential fields in Streamlit.",
  },
  {
    name: "JIRA_URL",
    value: "https://...",
    description: "Jira base URL only. No token is configured here.",
  },
  {
    name: "CONFLUENCE_URL",
    value: "https://...",
    description: "Confluence base URL only. No token is configured here.",
  },
  {
    name: "BITBUCKET_URL",
    value: "https://...",
    description: "Bitbucket base URL only. No token is configured here.",
  },
];

const requestRows = [
  {
    deployment: "Cloud",
    jira: "email + API token",
    confluence: "email + API token",
    bitbucket: "username/email + app password",
  },
  {
    deployment: "Local/DC",
    jira: "Personal Access Token",
    confluence: "Personal Access Token",
    bitbucket: "Personal Access Token",
  },
];

function SimpleTable({
  children,
}: {
  children: ReactNode;
}): JSX.Element {
  return <div className="card__body card__body--flush">{children}</div>;
}

export default function McpSetupTab() {
  return (
    <div className="stack" style={{ gap: "1rem" }}>
      <McpDeploymentSelector />

      <section className="card">
        <div className="card__header">
          <div>
            <div className="card__title">MCP Runtime</div>
            <div className="card__sub">
              MCP credentialsiz baslar; credential kullanici isteginde header olarak gelir.
            </div>
          </div>
        </div>
        <SimpleTable>
          <table className="table">
            <thead>
              <tr>
                <th>Degisken</th>
                <th>Deger</th>
                <th>Aciklama</th>
              </tr>
            </thead>
            <tbody>
              {runtimeRows.map((row) => (
                <tr key={row.name}>
                  <td className="mono text-sm">
                    <code>{row.name}</code>
                  </td>
                  <td className="mono text-sm">{row.value}</td>
                  <td className="text-sm">{row.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </SimpleTable>
      </section>

      <section className="card">
        <div className="card__header">
          <div>
            <div className="card__title">Kullanici Credential Akisi</div>
            <div className="card__sub">
              Streamlit chat ekraninda kullanici hangi servisi kullanacaksa o credential'i girer.
            </div>
          </div>
        </div>
        <SimpleTable>
          <table className="table">
            <thead>
              <tr>
                <th>Deployment</th>
                <th>Jira</th>
                <th>Confluence</th>
                <th>Bitbucket</th>
              </tr>
            </thead>
            <tbody>
              {requestRows.map((row) => (
                <tr key={row.deployment}>
                  <td>{row.deployment}</td>
                  <td>{row.jira}</td>
                  <td>{row.confluence}</td>
                  <td>{row.bitbucket}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </SimpleTable>
      </section>

      <section className="card">
        <div className="card__header">
          <div>
            <div className="card__title">Uyumluluk</div>
            <div className="card__sub">Jira, Confluence ve Bitbucket Cloud/Local DC hedefleri.</div>
          </div>
        </div>
        <SimpleTable>
          <table className="table">
            <thead>
              <tr>
                <th>Urun</th>
                <th>Deployment</th>
                <th>Destek</th>
              </tr>
            </thead>
            <tbody>
              {compatibilityRows.map((row) => (
                <tr key={`${row.product}-${row.deployment}`}>
                  <td>{row.product}</td>
                  <td>{row.deployment}</td>
                  <td>{row.support}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </SimpleTable>
      </section>
    </div>
  );
}
