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
