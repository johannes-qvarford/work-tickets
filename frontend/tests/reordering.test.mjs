import assert from "node:assert/strict";
import test from "node:test";
import {
  displayedDropTargetIndex,
  dropTargetIndex,
  handleSubtaskDragEnter,
  handleSubtaskDragOver,
  handleTicketDragEnter,
  handleTicketDragOver,
} from "../src/reordering.ts";

function dragEvent(type) {
  return {
    dataTransfer: { types: [type], dropEffect: "none" },
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
}

const tickets = [
  { id: 1, local_completed: false, category_id: 10 },
  { id: 2, local_completed: false, category_id: 20 },
  { id: 3, local_completed: false, category_id: 10 },
  { id: 4, local_completed: true, category_id: 10 },
];

test("drop uses the target's active-list position for downward, upward, and adjacent moves", () => {
  assert.equal(dropTargetIndex(tickets, 1, 3), 2);
  assert.equal(dropTargetIndex(tickets, 3, 1), 0);
  assert.equal(dropTargetIndex(tickets, 1, 2), 1);
  assert.equal(dropTargetIndex(tickets, 2, 1), 0);
});

test("drop rejects invalid, completed, and self targets", () => {
  assert.equal(dropTargetIndex(tickets, 1, 1), null);
  assert.equal(dropTargetIndex(tickets, 4, 1), null);
  assert.equal(dropTargetIndex(tickets, 1, 4), null);
  assert.equal(dropTargetIndex(tickets, 99, 1), null);
});

test("top-level dragenter feedback is applied immediately", () => {
  const event = dragEvent("application/x-work-tickets-ticket");
  const emitted = [];

  handleTicketDragEnter(event, {
    ticketId: 2,
    ticketCanDrag: true,
    draggingTicketId: 1,
    clearSubtaskDragOverState() { throw new Error("subtask state should not be cleared"); },
    onFeedback() { emitted.push(2); },
  });

  assert.deepEqual(emitted, [2]);
  assert.equal(event.prevented, true);
  assert.equal(event.stopped, true);
  assert.equal(event.dataTransfer.dropEffect, "move");
});

test("top-level dragover rejects self and non-draggable targets without feedback", () => {
  const feedback = [];
  const clearCalls = [];

  for (const options of [
    { ticketCanDrag: false, draggingTicketId: 1 },
    { ticketCanDrag: true, draggingTicketId: 2 },
  ]) {
    const event = dragEvent("application/x-work-tickets-ticket");
    handleTicketDragOver(event, {
      ticketId: 2,
      ...options,
      clearSubtaskDragOverState() { clearCalls.push(true); },
      onFeedback() { feedback.push(true); },
    });
    assert.equal(event.prevented, false);
  }

  assert.deepEqual(feedback, []);
  assert.deepEqual(clearCalls, []);
});

test("top-level dragover clears subtask feedback for a subtask drag", () => {
  const event = dragEvent("application/x-work-tickets-subtask");
  let clearCalls = 0;

  handleTicketDragOver(event, {
    ticketId: 2,
    ticketCanDrag: true,
    draggingTicketId: 1,
    clearSubtaskDragOverState() { clearCalls += 1; },
    onFeedback() { throw new Error("subtask drags must not emit ticket feedback"); },
  });

  assert.equal(clearCalls, 1);
  assert.equal(event.prevented, false);
});

test("subtask dragenter accepts a valid target and highlights it immediately", () => {
  const event = dragEvent("application/x-work-tickets-subtask");
  const state = [];

  handleSubtaskDragEnter(event, {
    parentId: 10,
    subtaskId: 12,
    subtaskCompleted: false,
    draggingParentId: 10,
    draggingSubtaskId: 11,
    clearSubtaskDragOverState() { throw new Error("valid subtask target should not clear state"); },
    onTarget() { state.push([10, 12]); },
  });

  assert.deepEqual(state, [[10, 12]]);
  assert.equal(event.prevented, true);
  assert.equal(event.stopped, true);
  assert.equal(event.dataTransfer.dropEffect, "move");
});

test("subtask dragover rejects completed, foreign-parent, and self targets", () => {
  const state = [];
  const clearCalls = [];

  for (const options of [
    { subtaskCompleted: true, draggingParentId: 10, draggingSubtaskId: 11 },
    { subtaskCompleted: false, draggingParentId: 20, draggingSubtaskId: 11 },
    { subtaskCompleted: false, draggingParentId: 10, draggingSubtaskId: 12 },
  ]) {
    const event = dragEvent("application/x-work-tickets-subtask");
    handleSubtaskDragOver(event, {
      parentId: 10,
      subtaskId: 12,
      ...options,
      clearSubtaskDragOverState() { clearCalls.push(true); },
      onTarget() { state.push("target"); },
    });
    assert.equal(event.prevented, false);
  }

  assert.deepEqual(state, []);
  assert.equal(clearCalls.length, 3);
});

test("filtered queue keeps the target's global active insertion index", () => {
  const displayed = (ticket) => ticket.category_id === 10;

  // Ticket 2 is active but hidden between the two displayed targets.
  assert.equal(displayedDropTargetIndex(tickets, 1, 3, displayed), 2);
  assert.equal(displayedDropTargetIndex(tickets, 3, 1, displayed), 0);
  assert.equal(displayedDropTargetIndex(tickets, 2, 3, displayed), null);
});
