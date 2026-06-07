"use client";

/**
 * ExternalLinks - renders Jira issue, Bitbucket PR and Confluence page links
 * extracted from the workflow's audit chain via the W3 deeplink helper.
 */

import type { ExternalLinksShape } from "../page";

interface ExternalLinksProps {
  links: ExternalLinksShape;
}

export default function ExternalLinks({ links }: ExternalLinksProps): JSX.Element {
  const hasLinks =
    links.jira_issue_url || links.bitbucket_pr_url || links.confluence_page_url;

  if (!hasLinks) return <></>;

  return (
    <section style={{ margin: "1rem 0", display: "flex", gap: "1rem", flexWrap: "wrap" }}>
      {links.jira_issue_url && (
        <a
          href={links.jira_issue_url}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.25rem",
            padding: "4px 10px",
            border: "1px solid #0052cc",
            borderRadius: "4px",
            color: "#0052cc",
            fontSize: "0.875rem",
            textDecoration: "none",
          }}
        >
           Jira Issue
        </a>
      )}
      {links.bitbucket_pr_url && (
        <a
          href={links.bitbucket_pr_url}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.25rem",
            padding: "4px 10px",
            border: "1px solid #0747a6",
            borderRadius: "4px",
            color: "#0747a6",
            fontSize: "0.875rem",
            textDecoration: "none",
          }}
        >
           Bitbucket PR
        </a>
      )}
      {links.confluence_page_url && (
        <a
          href={links.confluence_page_url}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.25rem",
            padding: "4px 10px",
            border: "1px solid #172b4d",
            borderRadius: "4px",
            color: "#172b4d",
            fontSize: "0.875rem",
            textDecoration: "none",
          }}
        >
           Confluence Page
        </a>
      )}
    </section>
  );
}
