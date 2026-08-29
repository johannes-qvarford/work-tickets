<script setup lang="ts">
import Button from "primevue/button";

export interface Category {
  id: number;
  name: string;
  components?: CategoryComponent[];
}

export interface CategoryComponent {
  id: number;
  name: string;
}

defineProps<{
  categories: Category[];
}>();

const selectedCategoryId = defineModel<number | null>({ required: true });
</script>

<template>
  <div class="category-options" role="group" aria-label="Ticket category">
    <Button
      type="button"
      label="Uncategorized"
      :outlined="selectedCategoryId !== null"
      :aria-pressed="selectedCategoryId === null"
      @click="selectedCategoryId = null"
    />
    <Button
      v-for="category in categories"
      :key="category.id"
      type="button"
      :label="category.name"
      :outlined="selectedCategoryId !== category.id"
      :aria-pressed="selectedCategoryId === category.id"
      @click="selectedCategoryId = category.id"
    />
  </div>
</template>
