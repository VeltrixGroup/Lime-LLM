<script setup>
defineProps({ large: Boolean });
</script>

<template>
  <span class="logo" :class="{ lg: large }">
    <svg aria-hidden="true" :width="large ? 18 : 16" :height="large ? 18 : 16" viewBox="0 0 24 24" fill="none" stroke="#0B0E0A"
         stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.5"/><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/></svg>
  </span>
</template>
