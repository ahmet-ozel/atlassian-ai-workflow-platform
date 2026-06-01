"use client";

import McpDeploymentSelector from "./McpDeploymentSelector";

type EnvVarRow = { name: string; required: string; description: string };

const compatibilityRows = [
  { product: "Jira", deployment: "Cloud", support: "Tam destek" },
  { product: "Jira", deployment: "Server / Data Center", support: "v8.14+" },
  { product: "Confluence", deployment: "Cloud", support: "Tam destek" },
  { product: "Confluence", deployment: "Server / Data Center", support: "v6.0+" },
  { product: "Bitbucket", deployment: "Cloud", support: "Tam destek" },
  { product: "Bitbucket", deployment: "Server / Data Center", support: "v7.0+" },
];

const jiraConfluenceCloudVars: EnvVarRow[] = [
  {
    name: "JIRA_URL",
    required: "Jira",
    description: "https://your-company.atlassian.net",
  },
  {
    name: "JIRA_USERNAME",
    required: "Jira",
    description: "Atlassian hesap e-postasi.",
  },
  {
    name: "JIRA_API_TOKEN",
    required: "Jira",
    description: "Atlassian API token. Confluence ile ayni token kullanilabilir.",
  },
  {
    name: "CONFLUENCE_URL",
    required: "Confluence",
    description: "https://your-company.atlassian.net/wiki",
  },
  {
    name: "CONFLUENCE_USERNAME",
    required: "Confluence",
    description: "Atlassian hesap e-postasi.",
  },
  {
    name: "CONFLUENCE_API_TOKEN",
    required: "Confluence",
    description: "Atlassian API token. Jira tokeniyle ayni olabilir.",
  },
];

const jiraConfluenceServerVars: EnvVarRow[] = [
  {
    name: "JIRA_URL",
    required: "Jira",
    description: "https://jira.your-company.com",
  },
  {
    name: "JIRA_PERSONAL_TOKEN",
    required: "Jira",
    description: "Jira profil ayarlarindan uretilen Personal Access Token.",
  },
  {
    name: "CONFLUENCE_URL",
    required: "Confluence",
    description: "https://confluence.your-company.com",
  },
  {
    name: "CONFLUENCE_PERSONAL_TOKEN",
    required: "Confluence",
    description: "Confluence profil ayarlarindan uretilen Personal Access Token.",
  },
];

const bitbucketCloudVars: EnvVarRow[] = [
  {
    name: "BITBUCKET_URL",
    required: "Evet",
    description: "Cloud base URL: https://bitbucket.org. Workspace ayri girilir.",
  },
  {
    name: "BITBUCKET_USERNAME",
    required: "Evet",
    description: "API token kullanirken Atlassian e-postasi; app password kullanirken Bitbucket kullanici adi.",
  },
  {
    name: "BITBUCKET_API_TOKEN",
    required: "Token secilirse",
    description: "Bitbucket tarafindan uretilen scoped API token. Jira/Confluence tokeni degildir.",
  },
  {
    name: "BITBUCKET_APP_PASSWORD",
    required: "Legacy secilirse",
    description: "Eski app password. Yeni kurulumda API token tercih edilir.",
  },
  {
    name: "BITBUCKET_WORKSPACE",
    required: "Evet",
    description: "Cloud icin workspace slug. Ornek: example_workspace.",
  },
];

const bitbucketServerVars: EnvVarRow[] = [
  {
    name: "BITBUCKET_URL",
    required: "Evet",
    description: "https://bitbucket.your-company.com",
  },
  {
    name: "BITBUCKET_PERSONAL_TOKEN",
    required: "Evet",
    description: "Bitbucket Server/DC profilinden uretilen Personal Access Token.",
  },
  {
    name: "BITBUCKET_PROJECT_KEY",
    required: "Opsiyonel",
    description: "Server/DC icin varsayilan project key; zorunlu degildir.",
  },
  {
    name: "BITBUCKET_SSL_VERIFY",
    required: "Opsiyonel",
    description: "Varsayilan true. Internal sertifika sorunlarinda bilincli degistirilir.",
  },
  {
    name: "BITBUCKET_TIMEOUT",
    required: "Opsiyonel",
    description: "Varsayilan 75 saniye.",
  },
];

const bitbucketScopeRows = [
  {
    scope: "Repositories: Read",
    use: "Repo listeleme, kaynak kodu, dosya icerigi",
    toolset: "bitbucket_repositories, bitbucket_source",
  },
  {
    scope: "Repositories: Write",
    use: "Repo olusturma, guncelleme, fork",
    toolset: "bitbucket_repositories",
  },
  {
    scope: "Pull requests: Read",
    use: "PR listeleme, PR detay, diff, yorum okuma",
    toolset: "bitbucket_pull_requests",
  },
  {
    scope: "Pull requests: Write",
    use: "PR olusturma, approve, merge, yorum ekleme",
    toolset: "bitbucket_pull_requests",
  },
  {
    scope: "Pipelines: Read / Write",
    use: "Pipeline run/log okuma, pipeline tetikleme/durdurma",
    toolset: "bitbucket_pipelines",
  },
  {
    scope: "Webhooks: Read / Write",
    use: "Webhook listeleme, olusturma, guncelleme",
    toolset: "bitbucket_webhooks",
  },
  {
    scope: "Projects / Workspace membership: Read",
    use: "Project, uye ve default reviewer bilgileri",
    toolset: "bitbucket_workspace",
  },
];

const smokePrompts = [
  "Find issues assigned to me in PROJ project",
  "Search Confluence for onboarding docs",
  "List open PRs in the backend repo",
  "Show the diff for PR #42",
  "Approve PR #42 and add a comment",
  "Trigger a pipeline on the main branch",
];

const setupSteps = [
  "Cloud veya Server/Data Center kurulum tipini sec.",
  "Jira ve Confluence icin Atlassian API token veya Personal Access Token olustur.",
  "Bitbucket icin Cloud API token ya da Server/DC Personal Access Token olustur.",
  "Credential alanlarini Streamlit Credentials ekranindan gir.",
  "OpenAI veya vLLM provider secimini LLM Providers ekranindan yap.",
  "Admin Servisler ekranindan atlassian-mcp servisinin calistigini kontrol et.",
  "Streamlit Chat icinde Jira, Confluence ve Bitbucket promptlariyla smoke test yap.",
];

function EnvTable({ rows }: { rows: EnvVarRow[] }) {
  return (
    <div className="card__body card__body--flush">
      <table className="table">
        <thead>
          <tr>
            <th>Degisken</th>
            <th>Gerekli</th>
            <th>Aciklama</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.name}>
              <td className="mono text-sm">
                <code>{row.name}</code>
              </td>
              <td className="text-sm">{row.required}</td>
              <td className="text-sm">{row.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CompactList({ items }: { items: readonly string[] }) {
  return (
    <ul
      style={{
        margin: 0,
        paddingLeft: "1.1rem",
        color: "var(--fg-muted)",
        fontSize: "0.88rem",
        lineHeight: 1.65,
      }}
    >
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

function CodeBlock({ children }: { children: string }) {
  return (
    <pre
      className="mono text-sm"
      style={{
        margin: 0,
        padding: "0.85rem 1rem",
        border: "1px solid var(--border)",
        borderRadius: 8,
        background: "var(--bg-subtle)",
        color: "var(--fg)",
        overflowX: "auto",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      <code>{children}</code>
    </pre>
  );
}

function SectionCard({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="card__header">
        <div>
          <div className="card__title">{title}</div>
          {subtitle && <div className="card__sub">{subtitle}</div>}
        </div>
      </div>
      <div className="card__body">{children}</div>
    </section>
  );
}

export default function McpSetupTab() {
  return (
    <div className="stack stack--lg">
      <div className="banner banner--info" role="note">
        <span className="banner__icon">i</span>
        <div className="banner__body">
          Aktif MCP hedefi <strong>jellythomas/mcp-atlassian-with-bitbucket</strong>.
          Jira, Confluence ve Bitbucket Cloud/Server/DC icin stateless MCP akisi
          kullanilir. Bu ekranda gercek token tutulmaz; sadece hangi bilginin
          nereden alinacagi anlatilir.
        </div>
      </div>

      <McpDeploymentSelector />

      <section className="grid-2" aria-label="MCP hizli ozet">
        <div className="stat-card">
          <div className="stat-card__label">Kimlik dogrulama</div>
          <div className="stat-card__value" style={{ fontSize: "1.15rem" }}>
            Header tabanli stateless
          </div>
          <div className="stat-card__delta">
            Credential degerleri Streamlit tarafindan MCP request header'larina eklenir.
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Varsayilan toolset</div>
          <div className="stat-card__value" style={{ fontSize: "1.15rem" }}>
            TOOLSETS=all
          </div>
          <div className="stat-card__delta">
            Jira, Confluence ve Bitbucket araclari ayni MCP servisinden gelir.
          </div>
        </div>
      </section>

      <SectionCard
        title="Kurulum Akisi"
        subtitle="Sifirdan kurulumda izlenecek en kisa ve net yol."
      >
        <ol
          style={{
            margin: 0,
            paddingLeft: "1.25rem",
            color: "var(--fg-muted)",
            fontSize: "0.9rem",
            lineHeight: 1.7,
          }}
        >
          {setupSteps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </SectionCard>

      <section className="card">
        <div className="card__header">
          <div>
            <div className="card__title">Uyumluluk</div>
            <div className="card__sub">Desteklenen Atlassian urunleri ve deployment tipleri.</div>
          </div>
        </div>
        <div className="card__body card__body--flush">
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
        </div>
      </section>

      <div className="grid-2">
        <section className="card">
          <div className="card__header">
            <div>
              <div className="card__title">Jira & Confluence Cloud</div>
              <div className="card__sub">
                Token: id.atlassian.com/manage-profile/security/api-tokens
              </div>
            </div>
          </div>
          <EnvTable rows={jiraConfluenceCloudVars} />
          <div className="card__footer text-sm muted">
            Tek Atlassian API token Jira ve Confluence icin birlikte kullanilabilir.
          </div>
        </section>

        <section className="card">
          <div className="card__header">
            <div>
              <div className="card__title">Jira & Confluence Server/DC</div>
              <div className="card__sub">Token profil ayarlarindan Personal Access Token olarak alinir.</div>
            </div>
          </div>
          <EnvTable rows={jiraConfluenceServerVars} />
        </section>
      </div>

      <div className="grid-2">
        <section className="card">
          <div className="card__header">
            <div>
              <div className="card__title">Bitbucket Cloud</div>
              <div className="card__sub">Onerilen yontem: scoped Bitbucket API Token.</div>
            </div>
          </div>
          <EnvTable rows={bitbucketCloudVars} />
          <div className="card__body">
            <CompactList
              items={[
                "Token Bitbucket icinden avatar > Personal settings > Security > API tokens ekranindan olusturulur.",
                "Workspace slug Cloud repo URL'sindeki bitbucket.org/{workspace}/{repo} bolumunden alinip ayri girilir.",
                "App password legacy yontemdir; yeni kurulumda API token secilmeli.",
              ]}
            />
          </div>
        </section>

        <section className="card">
          <div className="card__header">
            <div>
              <div className="card__title">Bitbucket Server/DC</div>
              <div className="card__sub">Token: Bitbucket profilinden Personal Access Token.</div>
            </div>
          </div>
          <EnvTable rows={bitbucketServerVars} />
          <div className="card__footer text-sm muted">
            PAT icin Project Read ve Repository Admin izinleri gerekir.
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card__header">
          <div>
            <div className="card__title">Bitbucket Cloud Scope Tablosu</div>
            <div className="card__sub">
              Read-only PR izleme icin minimum: Repositories: Read + Pull requests: Read.
            </div>
          </div>
        </div>
        <div className="card__body card__body--flush">
          <table className="table">
            <thead>
              <tr>
                <th>Scope</th>
                <th>Ne icin</th>
                <th>Toolset</th>
              </tr>
            </thead>
            <tbody>
              {bitbucketScopeRows.map((row) => (
                <tr key={row.scope}>
                  <td>{row.scope}</td>
                  <td>{row.use}</td>
                  <td className="mono text-sm">{row.toolset}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid-2">
        <SectionCard title="Ornek Credential Bloklari">
          <div className="stack">
            <CodeBlock>{`JIRA_URL=https://your-company.atlassian.net
JIRA_USERNAME=your.email@company.com
JIRA_API_TOKEN=ATATT3x...

CONFLUENCE_URL=https://your-company.atlassian.net/wiki
CONFLUENCE_USERNAME=your.email@company.com
CONFLUENCE_API_TOKEN=ATATT3x...`}</CodeBlock>
            <CodeBlock>{`BITBUCKET_URL=https://bitbucket.org
BITBUCKET_USERNAME=your.email@company.com
BITBUCKET_API_TOKEN=bb_pat_xxxxxxxxxxxx
BITBUCKET_WORKSPACE=your_workspace`}</CodeBlock>
          </div>
        </SectionCard>

        <SectionCard title="Chat Smoke Test Sorulari">
          <CompactList items={smokePrompts} />
        </SectionCard>
      </div>

      <SectionCard
        title="Global MCP Ayarlari"
        subtitle="Servis davranisini etkileyen ana ortam degiskenleri."
      >
        <div className="grid-3">
          <div>
            <div className="badge badge--brand">TOOLSETS</div>
            <p className="text-sm muted">
              <code>all</code> tum Jira, Confluence ve Bitbucket toolsetlerini acar.
              Daha dar kurulumlarda virgulle ayrilmis toolset listesi verilebilir.
            </p>
          </div>
          <div>
            <div className="badge badge--warn">READ_ONLY_MODE</div>
            <p className="text-sm muted">
              <code>false</code> yazma islemlerine izin verir. Sadece okuma testi
              icin <code>true</code> kullanilir.
            </p>
          </div>
          <div>
            <div className="badge badge--info">MCP_VERBOSE</div>
            <p className="text-sm muted">
              <code>true</code> detayli log acar. Normal calismada <code>false</code>
              kalabilir.
            </p>
          </div>
        </div>
      </SectionCard>
    </div>
  );
}
