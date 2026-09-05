# Work Tickets TODO

## Reviews and Jira Workflow

- [x] Add a separate `Reviews` page.

  On page switch, fetch Jira issues assigned to the configured Jira account, limited to the configured project and issue type, with the configured `In Review` status. Include Jira issues even when they have no local ticket equivalent.

  Add a manual refresh button. If Jira fails for one ticket, show an inline error for that ticket without preventing other tickets from loading.

- [x] Add configurable Jira workflow status settings.

  Add settings for `In Review`, `Ready to Merge`, and `Ready to Deploy`, using those values as the defaults. Status changes should find the Jira transition whose destination matches the configured status rather than attempting to update the status field directly.

  Treat an item already in the requested target status as successfully transitioned so retries are idempotent.

- [x] Add a `Ready to Merge` action to each Reviews item.

  There is no separate merge button. The action should perform the full review and merge workflow described below. It should transition the Jira issue to `Ready to Merge` and add the Jira comment `Tested and reviewed.`

## GitLab Merge Requests

- [x] Add GitLab settings.

  Add a GitLab base URL and user personal access token to application settings. Store and handle the token like the existing Jira credential.

- [x] Detect merge requests from Jira descriptions.

  For now, scan the Jira description for links matching the configured GitLab base URL, such as:

  ```text
  https://gitlab.example/group1/group2/repository/-/merge_requests/1234
  ```

  Extract the MR number and repository name. Omit repository groups from the displayed repository name. Detection may later be replaced with a Jira custom field.

- [x] Select an unambiguous MR for each ticket.

  If multiple MRs are found and more than one remains open, disable `Ready to Merge` and document that the action requires one unambiguous MR.

  If one open MR remains after filtering, use it and ignore closed MRs. If all MRs are closed, use the most recently updated MR. Disable the action when no MRs are found or when more than one MR remains after filtering.

- [x] Make the `Ready to Merge` action approve the selected MR when necessary.

  An MR that is already approved should be treated as successful.

- [x] Make the `Ready to Merge` action mark the selected MR as no longer a draft.

- [x] Resolve all unresolved discussion threads on the selected MR.

  Add the comment `Approved 👑` to each unresolved MR thread and mark each thread as resolved.

- [x] Merge the selected MR with squashing enabled.

  Wait for the MR to reach the merged state, then show a success or failure notification.

- [x] Complete the Jira update after a successful MR merge.

  Add a Jira comment containing a link to the MR commit, using the short SHA as the visible text, for example `Merged with <linked short SHA>`, and transition the Jira issue to `Ready to Deploy`.

- [x] Support partial failures and retries in the Ready to Merge workflow.

  Show failures inline on the affected Reviews item and provide a retry button. Both the initial action and retries must check the current Jira/GitLab state and safely skip steps that have already succeeded.
