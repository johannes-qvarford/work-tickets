<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import Button from "primevue/button";
import Card from "primevue/card";
import DatePicker from "primevue/datepicker";
import InputText from "primevue/inputtext";
import Message from "primevue/message";
import Select from "primevue/select";
import Textarea from "primevue/textarea";
import CategoryButtons, { type Category, type CategoryComponent } from "./components/CategoryButtons.vue";
import ComponentSelect from "./components/ComponentSelect.vue";
import RefineSessionCoordinator from "./components/RefineSessionCoordinator.vue";
import TicketCard, { type Ticket } from "./components/TicketCard.vue";
import { clearSubtaskDragState, displayedDropTargetIndex, dropTargetIndex } from "./reordering";

interface JiraConfig {
  base_url: string;
  browser_base_url: string;
  local_projects_directory: string;
  email: string;
  project_key: string;
  issue_type: string;
  completed_statuses: string;
}
interface State {
  tickets: Ticket[];
  categories: Category[];
  components: CategoryComponent[];
  jira_config: JiraConfig | null;
}
interface Review {
  key: string;
  summary: string;
  description: string;
  issue_type_name: string | null;
  status_name: string | null;
  local_ticket: { id: number; summary: string; parent_id: number | null } | null;
  error: string | null;
}
type TicketView = "focus" | "queue";

const state = ref<State>({ tickets: [], categories: [], components: [], jira_config: null });
const page = ref(window.location.hash.slice(1) || "tickets");
const categoryFilter = ref<number | null>(null);
const ticketView = ref<TicketView>(loadTicketView());
const notice = ref<{ severity: "success" | "error"; text: string } | null>(null);
const reviews = ref<Review[]>([]);
const reviewsLoading = ref(false);
const reviewsError = ref<string | null>(null);
const busy = ref(false);
const draggingTicketId = ref<number | null>(null);
const dragOverTicketId = ref<number | null>(null);
const newTicket = ref({ summary: "", description: "", notes: "", planned_date: null as Date | null, category_id: null as number | null, component: null as string | null, jira_reference: "" });
const newSubtasks = ref<Array<{ summary: string; description: string; planned_date: Date | null }>>([]);
const newCategory = ref("");
const newComponent = ref("");
const selectedComponentByCategory = ref<Record<number, number | null>>({});
const settings = ref({ base_url: "", browser_base_url: "", local_projects_directory: "", email: "", api_token: "", project_key: "", issue_type: "Task", completed_statuses: "Done", validate: false });

const visibleTickets = computed(() => categoryFilter.value === null ? state.value.tickets : state.value.tickets.filter((ticket) => ticket.category_id === categoryFilter.value));
const dueTickets = computed(() => visibleTickets.value.filter((ticket) => !ticket.local_completed && ticket.planned_date && ticket.planned_date <= dateValue(todayDate())));

function loadTicketView(): TicketView {
  try {
    return window.localStorage.getItem("work-tickets-view-mode") === "queue" ? "queue" : "focus";
  } catch {
    return "focus";
  }
}
function switchTicketView() {
  ticketView.value = ticketView.value === "focus" ? "queue" : "focus";
  try { window.localStorage.setItem("work-tickets-view-mode", ticketView.value); } catch { /* Storage may be unavailable. */ }
}
function go(next: string) { setPage(next); window.location.hash = next; notice.value = null; }
function setPage(next: string) {
  const changed = page.value !== next;
  page.value = next;
  if (changed && next === "reviews") void loadReviews();
}
function todayDate() {
  const today = new Date();
  return new Date(today.getFullYear(), today.getMonth(), today.getDate());
}
function dateValue(value: Date | null) {
  return value
    ? `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`
    : "";
}
async function request(url: string, init: RequestInit = {}) {
  const response = await fetch(url, { ...init, headers: { "Content-Type": "application/json", ...(init.headers || {}) } });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.message || "Request failed.");
  if (result.state) state.value = result.state;
  return result;
}
async function load() { const loaded = await (await fetch("/api/state")).json() as State; state.value = loaded; if (loaded.jira_config) Object.assign(settings.value, loaded.jira_config); }
async function loadReviews() {
  reviewsLoading.value = true;
  reviewsError.value = null;
  try {
    const response = await fetch("/api/reviews");
    const result = await response.json() as { ok: boolean; message?: string; reviews?: Review[] };
    if (!response.ok || !result.ok) throw new Error(result.message || "Could not load reviews.");
    reviews.value = result.reviews || [];
  } catch (error) {
    reviews.value = [];
    reviewsError.value = error instanceof Error ? error.message : "Could not load reviews.";
  } finally {
    reviewsLoading.value = false;
  }
}
async function run(action: () => Promise<void>, success = "Saved.") {
  busy.value = true; notice.value = null;
  try { await action(); notice.value = { severity: "success", text: success }; }
  catch (error) { notice.value = { severity: "error", text: error instanceof Error ? error.message : "Request failed." }; }
  finally { busy.value = false; }
}
async function createTicket() {
  await run(async () => {
    const result = await request("/api/tickets", { method: "POST", body: JSON.stringify({ ...newTicket.value, planned_date: dateValue(newTicket.value.planned_date) || null }) });
    if (result.created_id) for (const subtask of newSubtasks.value.filter((item) => item.summary.trim())) await request(`/api/tickets/${result.created_id}/subtasks`, { method: "POST", body: JSON.stringify({ ...subtask, planned_date: dateValue(subtask.planned_date) || null }) });
    newTicket.value = { summary: "", description: "", notes: "", planned_date: null, category_id: null, component: null, jira_reference: "" }; newSubtasks.value = [];
    go("tickets"); await load();
  }, "Ticket created.");
}
async function saveTicket(ticket: Ticket) { await run(() => request(`/api/tickets/${ticket.id}`, { method: "PUT", body: JSON.stringify({ summary: ticket.summary, description: ticket.description, notes: ticket.notes || "", planned_date: ticket.planned_date, category_id: ticket.category_id, component: ticket.component }) }), "Ticket updated."); }
async function saveSubtask(subtask: Ticket) { await run(() => request(`/api/subtasks/${subtask.id}`, { method: "PUT", body: JSON.stringify({ summary: subtask.summary, description: subtask.description, planned_date: subtask.planned_date }) }), "Subtask updated."); }
async function addSubtask(ticket: Ticket, draft: { summary: string; description: string; planned_date: string }) { await run(async () => { await request(`/api/tickets/${ticket.id}/subtasks`, { method: "POST", body: JSON.stringify({ ...draft, planned_date: draft.planned_date || null }) }); }, "Subtask added."); }
async function toggle(url: string) { await run(() => request(url, { method: "POST" }), "Updated."); }
async function remove(url: string) { if (!confirm("Delete this item?")) return; await run(() => request(url, { method: "DELETE" }), "Deleted."); }
async function saveCategory() { await run(async () => { await request("/api/categories", { method: "POST", body: JSON.stringify({ name: newCategory.value }) }); newCategory.value = ""; }, "Category saved."); }
async function deleteCategory(id: number) { if (!confirm("Delete this category? Tickets become uncategorized.")) return; await run(() => request(`/api/categories/${id}`, { method: "DELETE" }), "Category deleted."); }
async function saveComponent() { await run(async () => { await request("/api/components", { method: "POST", body: JSON.stringify({ name: newComponent.value }) }); newComponent.value = ""; }, "Component saved."); }
async function deleteComponent(id: number) { if (!confirm("Delete this component? Existing tickets keep their stored value.")) return; await run(() => request(`/api/components/${id}`, { method: "DELETE" }), "Component deleted."); }
async function assignComponent(category: Category) {
  const componentId = selectedComponentByCategory.value[category.id];
  if (componentId === null || componentId === undefined) return;
  await run(async () => { await request(`/api/categories/${category.id}/components`, { method: "POST", body: JSON.stringify({ component_id: componentId }) }); selectedComponentByCategory.value[category.id] = null; }, "Component assigned.");
}
async function removeCategoryComponent(categoryId: number, componentId: number) { await run(() => request(`/api/categories/${categoryId}/components/${componentId}`, { method: "DELETE" }), "Component removed from category."); }
async function moveCategoryComponent(categoryId: number, componentId: number, targetIndex: number) { await run(() => request(`/api/categories/${categoryId}/components/${componentId}/move?target_index=${targetIndex}`, { method: "POST" }), "Component order saved."); }
function availableComponents(category: Category) { return state.value.components.filter((component) => !(category.components || []).some((assigned) => assigned.id === component.id)); }
async function saveSettings() { await run(async () => { const result = await request("/api/settings/jira", { method: "PUT", body: JSON.stringify(settings.value) }); settings.value.api_token = ""; if (result.state.jira_config) Object.assign(settings.value, result.state.jira_config); }, "Jira settings saved."); }
async function sync(url: string) { await run(() => request(url, { method: "POST" }), "Jira sync complete."); }
async function moveTicket(ticketId: number, targetIndex: number) { await run(() => request(`/api/tickets/${ticketId}/move?target_index=${targetIndex}`, { method: "POST" }), "Ticket order saved."); }
async function moveSubtask(subtaskId: number, targetIndex: number) { await run(() => request(`/api/subtasks/${subtaskId}/move?target_index=${targetIndex}`, { method: "POST" }), "Subtask order saved."); }
function startTicketDrag(ticketId: number) { draggingTicketId.value = ticketId; dragOverTicketId.value = null; }
function endTicketDrag() { draggingTicketId.value = null; dragOverTicketId.value = null; }
function clearDragState() { endTicketDrag(); clearSubtaskDragState(); }
function overTicket(ticketId: number) { if (draggingTicketId.value !== null && draggingTicketId.value !== ticketId) dragOverTicketId.value = ticketId; }
function dropTicket(ticketId: number, afterTarget: boolean) {
  const sourceId = draggingTicketId.value;
  endTicketDrag();
  if (sourceId === null) return;
  const targetIndex = categoryFilter.value === null
    ? dropTargetIndex(state.value.tickets, sourceId, ticketId, afterTarget)
    : displayedDropTargetIndex(state.value.tickets, sourceId, ticketId, afterTarget, (ticket) => ticket.category_id === categoryFilter.value);
  if (targetIndex !== null) void moveTicket(sourceId, targetIndex);
}
function addDraftSubtask() { newSubtasks.value.push({ summary: "", description: "", planned_date: null }); }
function removeDraftSubtask(index: number) { if (!confirm("Delete this subtask?")) return; newSubtasks.value.splice(index, 1); }
function categoryName(id: number | null) { return state.value.categories.find((category) => category.id === id)?.name || "Uncategorized"; }
function reviewIssueUrl(key: string) {
  const baseUrl = state.value.jira_config?.browser_base_url.trim().replace(/\/+$/, "");
  return baseUrl ? `${baseUrl}/browse/${encodeURIComponent(key)}` : null;
}
onMounted(() => { load(); window.addEventListener("hashchange", () => { setPage(window.location.hash.slice(1) || "tickets"); }); if (page.value === "reviews") void loadReviews(); });
</script>

<template>
  <div class="shell" @drop="clearDragState" @dragend="clearDragState">
    <header class="topbar">
      <div><span class="eyebrow">PERSONAL WORKFLOW</span><h1>Work tickets</h1><p>Keep momentum across the work that matters.</p></div>
      <nav aria-label="Main navigation">
        <Button label="Tickets" icon="pi pi-list" :severity="page === 'tickets' ? undefined : 'secondary'" text @click="go('tickets')" />
        <Button label="Reviews" icon="pi pi-eye" :severity="page === 'reviews' ? undefined : 'secondary'" text @click="go('reviews')" />
        <Button label="Create" icon="pi pi-plus" :severity="page === 'create' ? undefined : 'secondary'" text @click="go('create')" />
        <Button label="Categories" icon="pi pi-tags" :severity="page === 'categories' ? undefined : 'secondary'" text @click="go('categories')" />
        <Button label="Settings" icon="pi pi-cog" :severity="page === 'settings' ? undefined : 'secondary'" text @click="go('settings')" />
      </nav>
    </header>
    <Message v-if="notice" :severity="notice.severity" closable @close="notice = null">{{ notice.text }}</Message>
    <RefineSessionCoordinator :tickets="state.tickets" />

    <main v-if="page === 'tickets'">
      <Card class="filter-card"><template #content><div class="filter-row"><label for="category-filter">Filter by category</label><Select id="category-filter" v-model="categoryFilter" :options="[{ id: null, name: 'All categories' }, ...state.categories]" optionLabel="name" optionValue="id" placeholder="All categories" /><Button type="button" class="view-toggle" severity="secondary" :label="ticketView === 'focus' ? 'Show Queue' : 'Show Focus'" :aria-label="ticketView === 'focus' ? 'Switch to Queue' : 'Switch to Focus'" :aria-controls="ticketView === 'focus' ? 'focus-section' : 'queue-section'" @click="switchTicketView" /><span>{{ visibleTickets.length }} tickets</span></div></template></Card>
      <section v-if="ticketView === 'focus'" id="focus-section" class="ticket-section"><div class="section-heading"><div><span class="eyebrow">FOCUS</span><h2>Today</h2></div><span class="muted">Due today and overdue</span></div><div v-if="dueTickets.length" class="ticket-grid"><TicketCard v-for="ticket in dueTickets" :key="`due-${ticket.id}`" :ticket="ticket" :categories="state.categories" :components="state.components" :browser-base-url="state.jira_config?.browser_base_url || ''" :category-name="categoryName(ticket.category_id)" @toggle="toggle(`/api/tickets/${ticket.id}/complete`)" @sync="sync(`/api/tickets/${ticket.id}/sync`)" @remove="remove(`/api/tickets/${ticket.id}`)" @save="saveTicket" @save-subtask="saveSubtask" @add-subtask="addSubtask" @toggle-subtask="toggle(`/api/subtasks/${$event}/complete`)" @remove-subtask="remove(`/api/subtasks/${$event}`)" @move-subtask="moveSubtask" /></div><div v-else class="empty-state"><i class="pi pi-check-circle"></i><strong>Nothing due right now</strong><span>Add the next useful thing when it arrives.</span></div></section>
      <section v-if="ticketView === 'queue'" id="queue-section" class="ticket-section"><div class="section-heading"><div><span class="eyebrow">QUEUE</span><h2>All tickets</h2></div><span class="muted">Drag active tickets to reorder.</span></div><div v-if="visibleTickets.length" class="ticket-grid"><TicketCard v-for="ticket in visibleTickets" :key="ticket.id" :ticket="ticket" :categories="state.categories" :components="state.components" :browser-base-url="state.jira_config?.browser_base_url || ''" :category-name="categoryName(ticket.category_id)" :reorderable="true" :dragging-ticket-id="draggingTicketId" :drag-over-ticket-id="dragOverTicketId" @ticket-drag-start="startTicketDrag" @ticket-drag-end="endTicketDrag" @ticket-drag-over="overTicket" @ticket-drop="dropTicket" @toggle="toggle(`/api/tickets/${ticket.id}/complete`)" @sync="sync(`/api/tickets/${ticket.id}/sync`)" @remove="remove(`/api/tickets/${ticket.id}`)" @save="saveTicket" @save-subtask="saveSubtask" @add-subtask="addSubtask" @toggle-subtask="toggle(`/api/subtasks/${$event}/complete`)" @remove-subtask="remove(`/api/subtasks/${$event}`)" @move-subtask="moveSubtask" /></div><div v-else class="empty-state"><i class="pi pi-inbox"></i><strong>No tickets yet</strong><span>Create a ticket to start your queue.</span></div></section>
     </main>

     <main v-else-if="page === 'reviews'" class="narrow">
       <section class="hero"><div><span class="eyebrow">JIRA WORKFLOW</span><h2>Reviews</h2><p>Issues assigned to you and waiting for review.</p></div><Button label="Refresh" icon="pi pi-refresh" severity="secondary" :loading="reviewsLoading" @click="loadReviews" /></section>
       <Message v-if="reviewsError" severity="error">{{ reviewsError }}</Message>
       <Card v-else-if="reviewsLoading && !reviews.length"><template #content><div class="empty-state"><i class="pi pi-spin pi-spinner"></i><span>Loading reviews...</span></div></template></Card>
       <section v-else-if="reviews.length" class="review-list">
         <Card v-for="review in reviews" :key="review.key" class="review-card"><template #content><div class="review-heading"><div><a v-if="reviewIssueUrl(review.key)" class="jira-key" :href="reviewIssueUrl(review.key) || undefined" target="_blank" rel="noopener noreferrer">{{ review.key }}</a><span v-else class="jira-key">{{ review.key }}</span><h3>{{ review.summary }}</h3></div><span class="review-status">{{ review.status_name || "In Review" }}</span></div><div class="ticket-meta">{{ review.issue_type_name || "Jira issue" }} · <span v-if="review.local_ticket">Local ticket: {{ review.local_ticket.summary }}</span><span v-else>Not in local tickets</span></div><Message v-if="review.error" severity="error" class="review-error">{{ review.error }}</Message><p v-if="review.description" class="review-description">{{ review.description }}</p></template></Card>
       </section>
       <Card v-else><template #content><div class="empty-state"><i class="pi pi-check-circle"></i><strong>No issues in review</strong><span>Nothing assigned to you is waiting for review.</span></div></template></Card>
     </main>

     <main v-else-if="page === 'create'" class="narrow">
      <section class="hero"><div><span class="eyebrow">BUILD</span><h2>Create a ticket</h2><p>Capture the outcome and break it into clear next steps.</p></div></section>
      <Card><template #content><form class="form" @submit.prevent="createTicket"><label>Summary or Jira reference<InputText v-model="newTicket.summary" required placeholder="What needs doing? Or WORK-123" /></label><div class="two-col"><label>Planned date<div class="date-control"><DatePicker v-model="newTicket.planned_date" dateFormat="yy-mm-dd" showIcon aria-label="Planned date" /><Button type="button" label="Today" text aria-label="Set planned date to today" @click="newTicket.planned_date = todayDate()" /><Button type="button" label="Unfocus" text aria-label="Remove planned date" :disabled="!newTicket.planned_date" @click="newTicket.planned_date = null" /></div></label><label>Category<CategoryButtons v-model="newTicket.category_id" :categories="state.categories" /></label></div><label>Component<ComponentSelect v-model="newTicket.component" :categories="state.categories" :components="state.components" :category-id="newTicket.category_id" /></label><label>Description<Textarea v-model="newTicket.description" rows="5" autoResize /></label><label>Jira import URL (optional)<InputText v-model="newTicket.jira_reference" placeholder="https://your-site/browse/WORK-123" /></label><div class="subtask-builder"><div class="section-heading"><h3>Subtasks</h3><Button type="button" label="Add step" icon="pi pi-plus" text @click="addDraftSubtask" /></div><div v-for="(subtask, index) in newSubtasks" :key="index" class="draft-row"><InputText v-model="subtask.summary" :placeholder="`Step ${index + 1}`" /><div class="date-control"><DatePicker v-model="subtask.planned_date" dateFormat="yy-mm-dd" showIcon :aria-label="`Planned date for step ${index + 1}`" /><Button type="button" label="Today" text :aria-label="`Set planned date for step ${index + 1}`" @click="subtask.planned_date = todayDate()" /><Button type="button" label="Unfocus" text :aria-label="`Remove planned date for step ${index + 1}`" :disabled="!subtask.planned_date" @click="subtask.planned_date = null" /></div><Button type="button" icon="pi pi-trash" severity="danger" text @click="removeDraftSubtask(index)" /></div></div><Button type="submit" label="Create ticket" icon="pi pi-check" :loading="busy" /></form></template></Card>
    </main>

    <main v-else-if="page === 'categories'" class="narrow">
      <section class="hero"><div><span class="eyebrow">ORGANIZE</span><h2>Categories</h2><p>Manage local labels and their component order.</p></div></section>
      <Card><template #content>
        <form class="inline-form" @submit.prevent="saveCategory"><InputText v-model="newCategory" placeholder="e.g. Client work" required /><Button type="submit" label="Add category" icon="pi pi-plus" /></form>
        <form class="inline-form component-create-form" @submit.prevent="saveComponent"><InputText v-model="newComponent" placeholder="e.g. payment-integration-app" required /><Button type="submit" label="Add component" icon="pi pi-plus" /></form>
        <div v-if="state.components.length" class="component-list"><div v-for="component in state.components" :key="component.id" class="component-item"><span><i class="pi pi-box"></i>{{ component.name }}</span><Button icon="pi pi-trash" severity="danger" text aria-label="Delete component" @click="deleteComponent(component.id)" /></div></div>
        <div v-if="state.categories.length" class="category-list">
          <div v-for="category in state.categories" :key="category.id" class="category-item category-with-components">
            <div class="category-heading"><span><i class="pi pi-tag"></i>{{ category.name }}</span><Button icon="pi pi-trash" severity="danger" text aria-label="Delete category" @click="deleteCategory(category.id)" /></div>
            <form class="inline-form component-assignment" @submit.prevent="assignComponent(category)"><Select v-model="selectedComponentByCategory[category.id]" :options="availableComponents(category)" optionLabel="name" optionValue="id" placeholder="Assign component" filter /><Button type="submit" label="Assign" icon="pi pi-link" :disabled="selectedComponentByCategory[category.id] === null || selectedComponentByCategory[category.id] === undefined" /></form>
            <div v-if="category.components?.length" class="assigned-components">
              <div v-for="(component, index) in category.components" :key="component.id" class="assigned-component"><span><i class="pi pi-box"></i>{{ component.name }}</span><div class="button-row"><Button icon="pi pi-chevron-up" text rounded :disabled="index === 0" aria-label="Move component up" @click="moveCategoryComponent(category.id, component.id, index - 1)" /><Button icon="pi pi-chevron-down" text rounded :disabled="index === category.components.length - 1" aria-label="Move component down" @click="moveCategoryComponent(category.id, component.id, index + 1)" /><Button icon="pi pi-times" severity="danger" text rounded aria-label="Remove component from category" @click="removeCategoryComponent(category.id, component.id)" /></div></div>
            </div>
            <span v-else class="muted category-components-empty">No components assigned.</span>
          </div>
        </div>
        <div v-else class="empty-state"><i class="pi pi-tags"></i><span>No categories yet.</span></div>
      </template></Card>
    </main>

     <main v-else class="narrow"><section class="hero"><div><span class="eyebrow">CONNECTIONS</span><h2>Application settings</h2><p>Configure the local Jira connection and local project root used by Refine.</p></div></section><Card><template #content><form class="form" @submit.prevent="saveSettings"><label>Jira API URL<InputText v-model="settings.base_url" type="url" required placeholder="https://api.atlassian.com/..." /></label><label>Jira browser URL<InputText v-model="settings.browser_base_url" type="url" placeholder="https://your-company.atlassian.net" /></label><label>Local projects directory<InputText v-model="settings.local_projects_directory" placeholder="/path/to/local/projects" /></label><div class="two-col"><label>Account email<InputText v-model="settings.email" type="email" required /></label><label>Project key<InputText v-model="settings.project_key" required /></label></div><div class="two-col"><label>Issue type<InputText v-model="settings.issue_type" required /></label><label>Completed statuses<InputText v-model="settings.completed_statuses" /></label></div><label>API token<InputText v-model="settings.api_token" type="password" placeholder="Leave blank to keep saved token" /></label><div class="button-row"><Button type="submit" label="Save settings" icon="pi pi-save" :loading="busy" /><Button type="button" label="Save & test connection" severity="secondary" @click="settings.validate = true; saveSettings(); settings.validate = false" /></div></form></template></Card></main>
    <Card v-if="page === 'create'" class="create-notes-card"><template #content><label class="form">Personal notes<Textarea v-model="newTicket.notes" rows="4" autoResize placeholder="Notes for your local workflow" /></label></template></Card>
  </div>
</template>
