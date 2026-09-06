import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ticketCard = readFileSync(new URL("../src/components/TicketCard.vue", import.meta.url), "utf8");

test("ticket and subtask edits have no explicit save buttons", () => {
  assert.doesNotMatch(ticketCard, /label="Save ticket"/);
  assert.doesNotMatch(ticketCard, /label="Save"/);
});

test("editable ticket fields persist on model updates", () => {
  for (const field of [
    "ticket.summary",
    "ticket.planned_date",
    "ticket.description",
    "ticket.notes",
    "ticket.category_id",
    "ticket.component",
  ]) {
    assert.match(ticketCard, new RegExp(`v-model="${field}"[^>]*@update:model-value="saveTicketChange"`));
  }
  assert.match(ticketCard, /function saveTicketChange\(\)[\s\S]*emit\("save", props\.ticket\)/);
});

test("editable subtask fields persist on model updates", () => {
  for (const field of ["subtask.summary", "subtask.planned_date", "subtask.description"]) {
    assert.match(ticketCard, new RegExp(`v-model="${field}"[^>]*@update:model-value="saveSubtaskChange\\(subtask\\)"`));
  }
  assert.match(ticketCard, /function saveSubtaskChange\(subtask: Ticket\)[\s\S]*emit\("saveSubtask", subtask\)/);
});
