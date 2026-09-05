export interface JiraSettings {
  base_url: string;
  browser_base_url: string;
  local_projects_directory: string;
  gitlab_base_url: string;
  email: string;
  api_token: string;
  gitlab_token: string;
  project_key: string;
  issue_type: string;
  completed_statuses: string;
  in_review_status: string;
  ready_to_merge_status: string;
  ready_to_deploy_status: string;
  validate: boolean;
}

export const gitlabBaseUrlGuidance = "Use the GitLab site root, including an installation context path when applicable. Do not include /api/v4 or a merge-request path. Examples: https://gitlab.com and https://gitlab.example.com/gitlab. An authenticated personal access token is required for connection testing and Reviews merge-request operations.";

export const jiraUrlGuidance = "The Jira API URL and Jira browser URL serve different purposes. The API URL is the base to which the REST path is appended. Valid examples include the Jira Cloud site https://company.atlassian.net, the Cloud API gateway https://api.atlassian.com/ex/jira/<cloud-id>, and a Server/Data Center context path https://jira.example.com/jira. The browser URL must be the site root rather than a /browse/... URL; it is used to construct issue links and Refine prompts.";

export function buildSettingsRequest(settings: JiraSettings, validate: boolean): RequestInit {
  return {
    method: "PUT",
    body: JSON.stringify({ ...settings, validate }),
  };
}

export function requestErrorMessage(
  result: { message?: unknown },
  fallback = "Request failed.",
): string {
  return typeof result.message === "string" && result.message ? result.message : fallback;
}
