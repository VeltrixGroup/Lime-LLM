<script setup>
import { status } from "../store";
</script>

<template>
  <p class="status" :class="{ show: status.show, error: status.isError }" role="status">
    {{ status.message }}
  </p>
</template>
