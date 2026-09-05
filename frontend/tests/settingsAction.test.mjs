import assert from "node:assert/strict";
import test from "node:test";
import { buildSettingsRequest, gitlabBaseUrlGuidance, jiraUrlGuidance, requestErrorMessage } from "../src/settingsAction.ts";

const settings = {
  base_url: "https://jira.example.test",
  browser_base_url: "",
  local_projects_directory: "",
  gitlab_base_url: "https://gitlab.example.test",
  email: "person@example.test",
  api_token: "jira-secret",
  gitlab_token: "gitlab-secret",
  project_key: "WORK",
  issue_type: "Task",
  completed_statuses: "Done",
  in_review_status: "In Review",
  ready_to_merge_status: "Ready to Merge",
  ready_to_deploy_status: "Ready to Deploy",
  validate: false,
};

function requestPayload(validate) {
  return JSON.parse(buildSettingsRequest(settings, validate).body);
}

test("normal settings save sends validate false", () => {
  assert.equal(requestPayload(false).validate, false);
});

test("Save & test connection sends validate true", () => {
  assert.equal(requestPayload(true).validate, true);
});

test("GitLab validation errors remain displayable through the request error path", () => {
  const message = "GitLab setup failed: GitLab returned HTTP 401: Unauthorized.";

  assert.equal(requestErrorMessage({ message }), message);
});

test("GitLab base URL guidance covers installation paths, URL exclusions, examples, and token use", () => {
  assert.match(gitlabBaseUrlGuidance, /site root/);
  assert.match(gitlabBaseUrlGuidance, /installation context path/);
  assert.match(gitlabBaseUrlGuidance, /\/api\/v4/);
  assert.match(gitlabBaseUrlGuidance, /merge-request path/);
  assert.match(gitlabBaseUrlGuidance, /https:\/\/gitlab\.com/);
  assert.match(gitlabBaseUrlGuidance, /https:\/\/gitlab\.example\.com\/gitlab/);
  assert.match(gitlabBaseUrlGuidance, /authenticated personal access token/);
  assert.match(gitlabBaseUrlGuidance, /connection testing and Reviews merge-request operations/);
});

test("Jira URL guidance distinguishes API and browser URLs and covers supported examples", () => {
  assert.match(jiraUrlGuidance, /API URL and Jira browser URL/);
  assert.match(jiraUrlGuidance, /base to which the REST path is appended/);
  assert.match(jiraUrlGuidance, /https:\/\/company\.atlassian\.net/);
  assert.match(jiraUrlGuidance, /https:\/\/api\.atlassian\.com\/ex\/jira\/<cloud-id>/);
  assert.match(jiraUrlGuidance, /https:\/\/jira\.example\.com\/jira/);
  assert.match(jiraUrlGuidance, /browser URL must be the site root rather than a \/browse\/\.\.\. URL/);
  assert.match(jiraUrlGuidance, /construct issue links and Refine prompts/);
});
