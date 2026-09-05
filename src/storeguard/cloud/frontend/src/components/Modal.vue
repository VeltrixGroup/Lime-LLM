<script setup>
import { onMounted, onUnmounted } from "vue";

const emit = defineEmits(["close"]);

function onKeydown(e) {
  if (e.key === "Escape") emit("close");
}

onMounted(() => document.addEventListener("keydown", onKeydown));
onUnmounted(() => document.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="modal-backdrop" @mousedown.self="$emit('close')">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="modal-head">
        <h3><slot name="title" /></h3>
        <button class="modal-close" type="button" aria-label="Close" @click="$emit('close')">
          ×
        </button>
      </div>
      <slot />
    </div>
  </div>
</template>
