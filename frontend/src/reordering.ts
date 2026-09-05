import { ref } from "vue";

export interface ReorderableItem {
  id: number;
  local_completed: boolean;
  category_id?: number | null;
}

export const draggingSubtaskId = ref<number | null>(null);
export const draggingSubtaskParentId = ref<number | null>(null);
export const dragOverSubtaskId = ref<number | null>(null);
export const dragOverSubtaskParentId = ref<number | null>(null);
export const dragOverSubtaskAfter = ref<boolean | null>(null);

export type ReorderDragEvent = Pick<DragEvent, "clientY" | "currentTarget" | "dataTransfer" | "preventDefault" | "stopPropagation">;

export interface TicketDragFeedbackOptions {
  ticketId: number;
  ticketCanDrag: boolean;
  draggingTicketId: number | null | undefined;
  clearSubtaskDragOverState: () => void;
  onFeedback: (afterTarget: boolean) => void;
}

export interface SubtaskDragFeedbackOptions {
  parentId: number;
  subtaskId: number;
  subtaskCompleted: boolean;
  draggingParentId: number | null;
  draggingSubtaskId: number | null;
  clearSubtaskDragOverState: () => void;
  onTarget: () => void;
  onFeedback: (afterTarget: boolean) => void;
}

export function clearSubtaskDragOverState() {
  dragOverSubtaskParentId.value = null;
  dragOverSubtaskId.value = null;
  dragOverSubtaskAfter.value = null;
}

export function clearSubtaskDragState() {
  draggingSubtaskParentId.value = null;
  draggingSubtaskId.value = null;
  clearSubtaskDragOverState();
}

export function isAfterDropTarget(pointerY: number, target: { top: number; height: number }): boolean {
  return pointerY > target.top + target.height / 2;
}

export function isTicketDrag(event: Pick<DragEvent, "dataTransfer">) {
  return event.dataTransfer?.types.includes("application/x-work-tickets-ticket") ?? false;
}

export function isSubtaskDrag(event: Pick<DragEvent, "dataTransfer">) {
  return event.dataTransfer?.types.includes("application/x-work-tickets-subtask") ?? false;
}

function applyDragOverFeedback(event: ReorderDragEvent, onFeedback: (afterTarget: boolean) => void) {
  event.preventDefault();
  event.stopPropagation();
  if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
  onFeedback(isAfterDropTarget(event.clientY, (event.currentTarget as HTMLElement).getBoundingClientRect()));
}

export function handleTicketDragOver(event: ReorderDragEvent, options: TicketDragFeedbackOptions) {
  if (isSubtaskDrag(event)) {
    options.clearSubtaskDragOverState();
    return;
  }
  if (!options.ticketCanDrag || !isTicketDrag(event) || options.draggingTicketId === options.ticketId) return;
  applyDragOverFeedback(event, options.onFeedback);
}

export function handleTicketDragEnter(event: ReorderDragEvent, options: TicketDragFeedbackOptions) {
  handleTicketDragOver(event, options);
}

export function handleSubtaskDragOver(event: ReorderDragEvent, options: SubtaskDragFeedbackOptions) {
  if (!isSubtaskDrag(event)) return;
  if (options.subtaskCompleted || options.draggingParentId !== options.parentId || options.draggingSubtaskId === options.subtaskId) {
    options.clearSubtaskDragOverState();
    return;
  }
  options.onTarget();
  applyDragOverFeedback(event, options.onFeedback);
}

export function handleSubtaskDragEnter(event: ReorderDragEvent, options: SubtaskDragFeedbackOptions) {
  handleSubtaskDragOver(event, options);
}

/** Return the server target index after removing the dragged active item. */
export function dropTargetIndex(
  items: readonly ReorderableItem[],
  sourceId: number,
  targetId: number,
  afterTarget: boolean,
): number | null {
  const activeItems = items.filter((item) => !item.local_completed);
  const sourceIndex = activeItems.findIndex((item) => item.id === sourceId);
  const targetIndex = activeItems.findIndex((item) => item.id === targetId);
  if (sourceIndex < 0 || targetIndex < 0 || sourceId === targetId) return null;

  const adjustedTargetIndex = targetIndex > sourceIndex ? targetIndex - 1 : targetIndex;
  return adjustedTargetIndex + (afterTarget ? 1 : 0);
}

/** Translate a displayed reorder into the global active insertion index. */
export function displayedDropTargetIndex(
  items: readonly ReorderableItem[],
  sourceId: number,
  targetId: number,
  afterTarget: boolean,
  isDisplayed: (item: ReorderableItem) => boolean,
): number | null {
  const displayedItems = items.filter((item) => !item.local_completed && isDisplayed(item));
  if (!displayedItems.some((item) => item.id === sourceId)) return null;
  if (!displayedItems.some((item) => item.id === targetId)) return null;
  return dropTargetIndex(items, sourceId, targetId, afterTarget);
}
