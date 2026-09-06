import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("ticket sync routes inbound for Jira-linked tickets and outbound otherwise", () => {
  assert.match(
    app,
    /function syncUrl\(ticket: Ticket\) \{ return `\/api\/tickets\/\$\{ticket\.id\}\/\$\{ticket\.jira_issue_key \? "sync-from-jira" : "sync"\}`; \}/,
  );
  assert.equal((app.match(/@sync="sync\(syncUrl\(ticket\)\)"/g) || []).length, 2);
});
