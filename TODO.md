# Work Tickets TODO

## Ticket List and Editing

- [x] Make the ticket list a vertical list.

  Each ticket should occupy the full available horizontal space of its container rather than being laid out as a multi-column grid.

- [x] Remove the ticket completion marker.

  The ticket state should remain clear from the existing styling and controls without an additional marker beside the title.

- [x] Make the ticket drag handle larger and move it to the leftmost side of the ticket, before the title.

- [x] Fix the ticket drag insertion edge case.

  Moving the item at position 2 before the item at position 1 should continue to work. Moving the item at position 1 after the item at position 2 must also swap the items correctly. The insertion index must account for removing the dragged item before calculating the final position.

- [x] Remove the ticket and subtask up/down arrow controls.

  Dragging is the supported reordering interaction.

- [x] Fix the expanded ticket edit layout.

  Clicking `Edit ticket and subtasks` must expand within the ticket/container width and never overflow horizontally. The layout must also remain usable on narrow screens.

- [x] Add personal notes to top-level tickets.

  Notes need a textarea when creating and editing a ticket. They are local-only, must never be sent to Jira, and must not be added to subtasks.

## Categories and Components

- [x] Replace ticket category selection dropdowns with category buttons.

  Include an explicit `Uncategorized` button. This applies to ticket creation and editing; the category filter may remain a dropdown.

- [x] Add local category-specific components.

  The Categories page should allow components such as `payment-integration-app` and `payment-provider-app` to be assigned to categories, reordered, and removed.

  Components are local metadata for now and may be mapped to Jira components in the future. A component may belong to multiple categories, with an independent order within each category. Components should be globally deduplicated in ticket selection.

  After selecting a ticket category, show category-associated components first, followed by components from other categories. Use a dropdown for component selection.

  If a component is deleted, existing tickets retain their stored component value, but the deleted component must not be selectable when creating or editing tickets.

## Refine Terminal

- [x] Add a `Refine` button backed by an xterm.js console.

  The button should be available for synced top-level tickets and synced subtasks, and should launch:

  ```text
  opencode --prompt "Refine <ticket browser URL>"
  ```

  Replace `<ticket browser URL>` with the configured Jira browser URL and the ticket's Jira key.

- [x] Add a local projects directory setting.

  The setting selects the root directory containing local projects. Refine should use `<local projects>/<component>` as the subprocess working directory and inherit the environment of the server process.

  Validate that the configured root path exists when saving settings. Disable `Refine` when the ticket has no local component, and report an inline error when the root/component directory does not exist at launch time.

- [x] Make Refine sessions resumable.

  Maintain one session per item, keyed by its Jira key regardless of whether it belongs to a top-level ticket or a subtask. The terminal should reconnect to an existing session after a browser refresh while the server process is running.

## Reviews and Jira Workflow

- [x] Add a separate `Reviews` page.

  On page switch, fetch Jira issues assigned to the configured Jira account, limited to the configured project and issue type, with the configured `In Review` status. Include Jira issues even when they have no local ticket equivalent.

  Add a manual refresh button. If Jira fails for one ticket, show an inline error for that ticket without preventing other tickets from loading.

- [x] Add configurable Jira workflow status settings.

  Add settings for `In Review`, `Ready to Merge`, and `Ready to Deploy`, using those values as the defaults. Status changes should find the Jira transition whose destination matches the configured status rather than attempting to update the status field directly.

  Treat an item already in the requested target status as successfully transitioned so retries are idempotent.

- [ ] Add a `Ready to Merge` action to each Reviews item.

  There is no separate merge button. The action should perform the full review and merge workflow described below. It should transition the Jira issue to `Ready to Merge` and add the Jira comment `Tested and reviewed.`

## GitLab Merge Requests

- [ ] Add GitLab settings.

  Add a GitLab base URL and user personal access token to application settings. Store and handle the token like the existing Jira credential.

- [ ] Detect merge requests from Jira descriptions.

  For now, scan the Jira description for links matching the configured GitLab base URL, such as:

  ```text
  https://gitlab.example/group1/group2/repository/-/merge_requests/1234
  ```

  Extract the MR number and repository name. Omit repository groups from the displayed repository name. Detection may later be replaced with a Jira custom field.

- [ ] Select an unambiguous MR for each ticket.

  If multiple MRs are found and more than one remains open, disable `Ready to Merge` and document that the action requires one unambiguous MR.

  If one open MR remains after filtering, use it and ignore closed MRs. If all MRs are closed, use the most recently updated MR. Disable the action when no MRs are found or when more than one MR remains after filtering.

- [ ] Make the `Ready to Merge` action approve the selected MR when necessary.

  An MR that is already approved should be treated as successful.

- [ ] Make the `Ready to Merge` action mark the selected MR as no longer a draft.

- [ ] Resolve all unresolved discussion threads on the selected MR.

  Add the comment `Approved 👑` to each unresolved MR thread and mark each thread as resolved.

- [ ] Merge the selected MR with squashing enabled.

  Wait for the MR to reach the merged state, then show a success or failure notification.

- [ ] Complete the Jira update after a successful MR merge.

  Add a Jira comment containing a link to the MR commit, using the short SHA as the visible text, for example `Merged with <linked short SHA>`, and transition the Jira issue to `Ready to Deploy`.

- [ ] Support partial failures and retries in the Ready to Merge workflow.

  Show failures inline on the affected Reviews item and provide a retry button. Both the initial action and retries must check the current Jira/GitLab state and safely skip steps that have already succeeded.
