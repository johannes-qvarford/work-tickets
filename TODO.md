# Work Tickets TODO

## Ticket List and Editing

- [ ] Fix drag-and-drop insertion feedback for top-level tickets and subtasks.

  When the pointer enters a valid active drop target, highlight that ticket or subtask immediately. Do not require an arbitrary additional movement before showing feedback. Use the pointer position within the target to indicate whether the item will be inserted before or after it, and keep the same behavior when the queue is filtered.

- [ ] Allow local-only edits to synced tickets.

  After a ticket is synced to Jira, disable and visibly gray out its summary and description fields. Keep local-only fields editable: personal notes on top-level tickets, planned date, category, and component. The API should treat unchanged summary and description values as unchanged, save local-only changes without calling Jira, and reject attempted changes to those Jira-owned fields with a field-specific error. Saving changes to only local fields must succeed even when Jira is unavailable. If a save does require Jira and fails, display the actual Jira error rather than treating every failure as an unexplained 422. Add coverage for both the UI state and the API behavior.

- [ ] Fix new subtask creation returning HTTP 500.

  Creating a subtask currently passes `NULL` to the shared `tickets.notes` column, which is `NOT NULL` in the migrated development database. Subtasks must continue to have no notes field in the API or UI, while creation from both the ticket-creation flow and the ticket-edit flow succeeds. Do not regress existing tickets or the no-notes-on-subtasks invariant. Add a regression test using the migrated schema.

## Refine Terminal

- [ ] Fix the Refine terminal when a session is started.

  A WebSocket remaining open until the terminal is closed is expected; the issue is that the connected terminal currently shows no usable session and does not accept input. For a synced ticket with a valid local component, local projects directory, and Jira browser URL, launch `opencode --prompt "Refine <Jira issue URL>"`, display the process output, and forward terminal input to the process. Show a user-facing error when the command cannot start or the working directory is invalid. Keep one session per Jira key and reconnect to it after a browser refresh while the process is still running. Add a regression test for initial output, input forwarding, and reconnect behavior.

## Reviews and Jira

- [ ] Migrate Jira Reviews search away from the removed endpoint.

  Jira Cloud must use the supported `/rest/api/3/search/jql` contract instead of the removed search endpoint, including its request and pagination behavior. Preserve the existing project, issue type, status, and assignee filters and include all result pages. Retain the current Jira Server/Data Center behavior where that endpoint remains supported. Add client tests for Cloud pagination and for the existing Server/Data Center path.

## Settings

- [ ] Validate the configured GitLab connection when requested.

  `Save & test connection` must always validate Jira and must also validate GitLab when both a GitLab base URL and token are configured. Use an authenticated, non-mutating GitLab API request and report GitLab-specific failures without saving a connection that failed validation. If only one GitLab credential is supplied, show an actionable validation error instead of silently reporting success. Keep GitLab optional when neither GitLab field is configured.

- [ ] Add GitLab URL guidance to Settings.

  Explain that the field accepts the GitLab site root, including an installation context path when applicable, and must not include `/api/v4` or a merge-request path. Include examples such as `https://gitlab.com` and `https://gitlab.example.com/gitlab`, and state that an authenticated personal access token is required for connection testing and Reviews merge-request operations.

- [ ] Add Jira URL guidance to Settings.

  Explain the difference between the Jira API URL and the Jira browser URL. Show valid examples for a Jira Cloud site (`https://company.atlassian.net`), the Cloud API gateway (`https://api.atlassian.com/ex/jira/<cloud-id>`), and a Server/Data Center context path (`https://jira.example.com/jira`). State that the API URL is the base to which the REST path is appended, the browser URL must be the site root rather than a `/browse/...` URL, and the browser URL is used to construct issue links and Refine prompts.
