"use client";

import { useEffect, useMemo, useState } from "react";
import type { CSSProperties, Dispatch, SetStateAction } from "react";

export type DeploymentMode = "cloud" | "dc";

type McpStartupProfile = "full" | "read-only" | "lite" | "custom";
type ToolAccess = "read" | "write";
type ToolProduct = "Jira" | "Confluence" | "Bitbucket";

type ToolDefinition = {
  name: string;
  product: ToolProduct;
  access: ToolAccess;
  lite?: true;
  deployment?: DeploymentMode;
};

type Props = {
  availableKeys: Set<string>;
  mode: DeploymentMode;
  values: Record<string, string>;
  setValues: Dispatch<SetStateAction<Record<string, string>>>;
};

const noToolsSentinel = "__none__";

const profileOptions: Array<{ value: McpStartupProfile; label: string }> = [
  { value: "lite", label: "Lite" },
  { value: "full", label: "Full" },
  { value: "read-only", label: "Read only" },
  { value: "custom", label: "Custom" },
];

const mcpTools: ToolDefinition[] = [
  { name: "jira_get_user_profile", product: "Jira", access: "read", lite: true },
  { name: "jira_get_issue_watchers", product: "Jira", access: "read", lite: true },
  { name: "jira_add_watcher", product: "Jira", access: "write" },
  { name: "jira_remove_watcher", product: "Jira", access: "write" },
  { name: "jira_get_issue", product: "Jira", access: "read", lite: true },
  { name: "jira_search", product: "Jira", access: "read", lite: true },
  { name: "jira_search_fields", product: "Jira", access: "read", lite: true },
  { name: "jira_get_field_options", product: "Jira", access: "read", lite: true },
  { name: "jira_get_project_issues", product: "Jira", access: "read", lite: true },
  { name: "jira_get_transitions", product: "Jira", access: "read", lite: true },
  { name: "jira_get_worklog", product: "Jira", access: "read", lite: true },
  { name: "jira_download_attachments", product: "Jira", access: "read", lite: true },
  { name: "jira_get_issue_images", product: "Jira", access: "read", lite: true },
  { name: "jira_get_agile_boards", product: "Jira", access: "read", lite: true },
  { name: "jira_get_board_issues", product: "Jira", access: "read", lite: true },
  { name: "jira_get_sprints_from_board", product: "Jira", access: "read", lite: true },
  { name: "jira_get_sprint_issues", product: "Jira", access: "read", lite: true },
  { name: "jira_get_link_types", product: "Jira", access: "read", lite: true },
  { name: "jira_create_issue", product: "Jira", access: "write" },
  { name: "jira_batch_create_issues", product: "Jira", access: "write" },
  { name: "jira_batch_get_changelogs", product: "Jira", access: "read", lite: true, deployment: "cloud" },
  { name: "jira_update_issue", product: "Jira", access: "write" },
  { name: "jira_delete_issue", product: "Jira", access: "write" },
  { name: "jira_add_comment", product: "Jira", access: "write" },
  { name: "jira_edit_comment", product: "Jira", access: "write" },
  { name: "jira_add_worklog", product: "Jira", access: "write" },
  { name: "jira_link_to_epic", product: "Jira", access: "write" },
  { name: "jira_create_issue_link", product: "Jira", access: "write" },
  { name: "jira_create_remote_issue_link", product: "Jira", access: "write" },
  { name: "jira_remove_issue_link", product: "Jira", access: "write" },
  { name: "jira_transition_issue", product: "Jira", access: "write" },
  { name: "jira_create_sprint", product: "Jira", access: "write" },
  { name: "jira_update_sprint", product: "Jira", access: "write" },
  { name: "jira_add_issues_to_sprint", product: "Jira", access: "write" },
  { name: "jira_get_project_versions", product: "Jira", access: "read", lite: true },
  { name: "jira_get_project_components", product: "Jira", access: "read", lite: true },
  { name: "jira_get_all_projects", product: "Jira", access: "read", lite: true },
  { name: "jira_get_service_desk_for_project", product: "Jira", access: "read", lite: true, deployment: "dc" },
  { name: "jira_get_service_desk_queues", product: "Jira", access: "read", lite: true, deployment: "dc" },
  { name: "jira_get_queue_issues", product: "Jira", access: "read", lite: true, deployment: "dc" },
  { name: "jira_create_version", product: "Jira", access: "write" },
  { name: "jira_batch_create_versions", product: "Jira", access: "write" },
  { name: "jira_get_issue_proforma_forms", product: "Jira", access: "read", lite: true },
  { name: "jira_get_proforma_form_details", product: "Jira", access: "read", lite: true },
  { name: "jira_update_proforma_form_answers", product: "Jira", access: "write" },
  { name: "jira_get_issue_dates", product: "Jira", access: "read", lite: true },
  { name: "jira_get_issue_sla", product: "Jira", access: "read", lite: true },
  { name: "jira_get_issue_development_info", product: "Jira", access: "read", lite: true },
  { name: "jira_get_issues_development_info", product: "Jira", access: "read", lite: true },
  { name: "confluence_search", product: "Confluence", access: "read", lite: true },
  { name: "confluence_get_page", product: "Confluence", access: "read", lite: true },
  { name: "confluence_get_page_children", product: "Confluence", access: "read", lite: true },
  { name: "confluence_get_comments", product: "Confluence", access: "read", lite: true },
  { name: "confluence_get_labels", product: "Confluence", access: "read", lite: true },
  { name: "confluence_add_label", product: "Confluence", access: "write" },
  { name: "confluence_create_page", product: "Confluence", access: "write" },
  { name: "confluence_update_page", product: "Confluence", access: "write" },
  { name: "confluence_delete_page", product: "Confluence", access: "write" },
  { name: "confluence_move_page", product: "Confluence", access: "write" },
  { name: "confluence_add_comment", product: "Confluence", access: "write" },
  { name: "confluence_reply_to_comment", product: "Confluence", access: "write" },
  { name: "confluence_search_user", product: "Confluence", access: "read", lite: true },
  { name: "confluence_get_page_history", product: "Confluence", access: "read", lite: true },
  { name: "confluence_get_page_diff", product: "Confluence", access: "read", lite: true },
  { name: "confluence_get_page_views", product: "Confluence", access: "read", lite: true, deployment: "cloud" },
  { name: "confluence_upload_attachment", product: "Confluence", access: "write" },
  { name: "confluence_upload_attachments", product: "Confluence", access: "write" },
  { name: "confluence_get_attachments", product: "Confluence", access: "read", lite: true },
  { name: "confluence_download_attachment", product: "Confluence", access: "read", lite: true },
  { name: "confluence_download_content_attachments", product: "Confluence", access: "read", lite: true },
  { name: "confluence_delete_attachment", product: "Confluence", access: "write" },
  { name: "confluence_get_page_images", product: "Confluence", access: "read", lite: true },
  { name: "bitbucket_list_repositories", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_repository", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_create_repository", product: "Bitbucket", access: "write" },
  { name: "bitbucket_update_repository", product: "Bitbucket", access: "write" },
  { name: "bitbucket_delete_repository", product: "Bitbucket", access: "write" },
  { name: "bitbucket_fork_repository", product: "Bitbucket", access: "write" },
  { name: "bitbucket_list_forks", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_pull_requests", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_pull_request", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_pull_request_diff", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_create_pull_request", product: "Bitbucket", access: "write" },
  { name: "bitbucket_merge_pull_request", product: "Bitbucket", access: "write" },
  { name: "bitbucket_add_pull_request_comment", product: "Bitbucket", access: "write" },
  { name: "bitbucket_update_pull_request", product: "Bitbucket", access: "write" },
  { name: "bitbucket_decline_pull_request", product: "Bitbucket", access: "write" },
  { name: "bitbucket_approve_pull_request", product: "Bitbucket", access: "write" },
  { name: "bitbucket_unapprove_pull_request", product: "Bitbucket", access: "write" },
  { name: "bitbucket_request_changes_pull_request", product: "Bitbucket", access: "write" },
  { name: "bitbucket_get_pull_request_commits", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_pull_request_comments", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_add_inline_comment", product: "Bitbucket", access: "write" },
  { name: "bitbucket_reply_to_comment", product: "Bitbucket", access: "write" },
  { name: "bitbucket_update_comment", product: "Bitbucket", access: "write" },
  { name: "bitbucket_delete_comment", product: "Bitbucket", access: "write" },
  { name: "bitbucket_list_pull_request_statuses", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_branches", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_create_branch", product: "Bitbucket", access: "write" },
  { name: "bitbucket_delete_branch", product: "Bitbucket", access: "write" },
  { name: "bitbucket_get_branching_model", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_branch_restrictions", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_commits", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_commit", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_compare_commits", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_commit_statuses", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_create_commit_status", product: "Bitbucket", access: "write" },
  { name: "bitbucket_add_commit_comment", product: "Bitbucket", access: "write" },
  { name: "bitbucket_get_file_content", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_browse_directory", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_search_code", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_file_blame", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_file_history", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_tags", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_create_tag", product: "Bitbucket", access: "write" },
  { name: "bitbucket_delete_tag", product: "Bitbucket", access: "write" },
  { name: "bitbucket_list_pipelines", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_pipeline", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_trigger_pipeline", product: "Bitbucket", access: "write" },
  { name: "bitbucket_stop_pipeline", product: "Bitbucket", access: "write" },
  { name: "bitbucket_get_pipeline_step_log", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_pipeline_variables", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_create_pipeline_variable", product: "Bitbucket", access: "write" },
  { name: "bitbucket_get_pipeline_config", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_environments", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_deployment", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_deployment_releases", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_webhooks", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_create_webhook", product: "Bitbucket", access: "write" },
  { name: "bitbucket_update_webhook", product: "Bitbucket", access: "write" },
  { name: "bitbucket_delete_webhook", product: "Bitbucket", access: "write" },
  { name: "bitbucket_list_workspaces", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_workspace", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_list_workspace_members", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_get_default_reviewers", product: "Bitbucket", access: "read", lite: true },
  { name: "bitbucket_add_default_reviewer", product: "Bitbucket", access: "write" },
];

const fieldRowStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.25rem",
  marginBottom: "0.75rem",
};

const labelStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.25rem",
  fontWeight: 600,
};

const inputStyle: CSSProperties = {
  padding: "0.45rem 0.55rem",
  border: "1px solid #ccc",
  borderRadius: 4,
  fontFamily: "monospace",
};

const helpTextStyle: CSSProperties = {
  color: "#666",
  fontSize: "0.78rem",
  fontWeight: 400,
  lineHeight: 1.35,
};

const toolbarStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: "0.45rem",
  margin: "0.35rem 0 0.7rem",
};

const toolGridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
  gap: "0.35rem",
  maxHeight: 320,
  overflow: "auto",
  padding: "0.5rem",
  border: "1px solid #ddd",
  borderRadius: 4,
};

export function AtlassianMcpToolProfileFields({
  availableKeys,
  mode,
  values,
  setValues,
}: Props): JSX.Element | null {
  const visibleTools = useMemo(() => toolsForMode(mode), [mode]);
  const liteTools = useMemo(() => liteToolsForMode(mode), [mode]);
  const [profile, setProfile] = useState<McpStartupProfile>(() =>
    profileFromValues(values, mode),
  );
  const [selectedToolNames, setSelectedToolNames] = useState<string[]>(() => {
    const fromValues = parseToolCsv(values.ENABLED_TOOLS);
    return fromValues.length > 0 ? fromValues : liteToolsForMode(mode);
  });

  useEffect(() => {
    const visibleNames = new Set(visibleTools.map((tool) => tool.name));
    if (profile === "lite") {
      setSelectedToolNames((current) =>
        sameStringArray(current, liteTools) ? current : liteTools,
      );
      return;
    }
    setSelectedToolNames((current) => {
      const filtered = current.filter((name) => visibleNames.has(name));
      return sameStringArray(filtered, current) ? current : filtered;
    });
  }, [liteTools, profile, visibleTools]);

  useEffect(() => {
    setValues((current) => {
      const next = { ...current };
      const readOnly = profile === "read-only";
      const enabledTools =
        profile === "full" || profile === "read-only"
          ? ""
          : selectedToolNames.length > 0
            ? selectedToolNames.join(",")
            : noToolsSentinel;
      if (availableKeys.has("TOOLSETS")) next.TOOLSETS = "all";
      if (availableKeys.has("ENABLED_TOOLS")) next.ENABLED_TOOLS = enabledTools;
      if (availableKeys.has("READ_ONLY")) next.READ_ONLY = String(readOnly);
      if (availableKeys.has("READ_ONLY_MODE")) next.READ_ONLY_MODE = String(readOnly);
      return shallowEqual(current, next) ? current : next;
    });
  }, [availableKeys, profile, selectedToolNames, setValues]);

  if (!availableKeys.has("TOOLSETS")) return null;

  const selectedCount = selectedToolNames.length;
  const customVisible = profile === "custom";

  return (
    <div>
      <div style={fieldRowStyle}>
        <label style={labelStyle}>
          <span>Startup profile</span>
          <select
            value={profile}
            onChange={(ev) => {
              const nextProfile = ev.currentTarget.value as McpStartupProfile;
              setProfile(nextProfile);
              if (nextProfile === "lite") setSelectedToolNames(liteTools);
              if (nextProfile === "custom" && selectedToolNames.length === 0) {
                setSelectedToolNames(liteTools);
              }
            }}
            style={inputStyle}
          >
            {profileOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <span style={helpTextStyle}>{profileHelp(profile, selectedCount)}</span>
        </label>
      </div>

      {customVisible && (
        <div style={fieldRowStyle}>
          <div style={toolbarStyle}>
            <SmallButton onClick={() => setSelectedToolNames(liteTools)}>
              Lite set
            </SmallButton>
            <SmallButton
              onClick={() =>
                setSelectedToolNames(
                  visibleTools
                    .filter((tool) => tool.access === "read")
                    .map((tool) => tool.name),
                )
              }
            >
              Read tools
            </SmallButton>
            <SmallButton
              onClick={() => setSelectedToolNames(visibleTools.map((tool) => tool.name))}
            >
              All listed
            </SmallButton>
            <SmallButton onClick={() => setSelectedToolNames([])}>Clear</SmallButton>
          </div>
          <div style={toolGridStyle}>
            {visibleTools.map((tool) => (
              <label key={tool.name} style={{ display: "flex", gap: "0.45rem" }}>
                <input
                  type="checkbox"
                  checked={selectedToolNames.includes(tool.name)}
                  onChange={(ev) => {
                    const isChecked = ev.currentTarget.checked;
                    setSelectedToolNames((current) =>
                      isChecked
                        ? sortedUnique([...current, tool.name])
                        : current.filter((name) => name !== tool.name),
                    );
                  }}
                />
                <span>
                  <code>{tool.name}</code>
                  <span style={helpTextStyle}>
                    {" "}
                    {tool.product} / {tool.access}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SmallButton({
  children,
  onClick,
}: {
  children: React.ReactNode;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "0.32rem 0.55rem",
        border: "1px solid #bbb",
        borderRadius: 4,
        background: "#fff",
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

export function liteToolCsvForMode(mode: DeploymentMode): string {
  return liteToolsForMode(mode).join(",");
}

function profileFromValues(
  values: Record<string, string>,
  mode: DeploymentMode,
): McpStartupProfile {
  const enabledTools = parseToolCsv(values.ENABLED_TOOLS);
  if (enabledTools.length > 0) {
    return sameStringArray(sortedUnique(enabledTools), sortedUnique(liteToolsForMode(mode)))
      ? "lite"
      : "custom";
  }
  if (isTruthy(values.READ_ONLY_MODE) || isTruthy(values.READ_ONLY)) {
    return "read-only";
  }
  return "lite";
}

function profileHelp(profile: McpStartupProfile, selectedCount: number): string {
  if (profile === "full") return "TOOLSETS=all, every registered tool is exposed.";
  if (profile === "read-only") return "All read tools are exposed; write tools are blocked by READ_ONLY_MODE.";
  if (profile === "lite") return "Only the curated read-tool profile is exposed.";
  return `${selectedCount} selected tool${selectedCount === 1 ? "" : "s"} will be exposed.`;
}

function toolsForMode(mode: DeploymentMode): ToolDefinition[] {
  return mcpTools.filter((tool) => tool.deployment == null || tool.deployment === mode);
}

function liteToolsForMode(mode: DeploymentMode): string[] {
  return toolsForMode(mode)
    .filter((tool) => tool.lite === true)
    .map((tool) => tool.name);
}

function parseToolCsv(value: string | undefined): string[] {
  return sortedUnique((value ?? "").split(",").map((item) => item.trim()).filter(Boolean));
}

function sortedUnique(values: string[]): string[] {
  return Array.from(new Set(values)).sort();
}

function sameStringArray(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((value, index) => value === right[index]);
}

function isTruthy(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((value ?? "").trim().toLowerCase());
}

function shallowEqual(left: Record<string, string>, right: Record<string, string>): boolean {
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => left[key] === right[key]);
}
