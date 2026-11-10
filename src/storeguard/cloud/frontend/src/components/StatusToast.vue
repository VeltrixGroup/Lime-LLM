<script setup>
import { status } from "../store";
</script>

<template>
  <transition name="fade">
    <div v-if="status.show" class="toast" :class="{ err: status.isError }" role="status">{{ status.message }}</div>
  </transition>
</template>
