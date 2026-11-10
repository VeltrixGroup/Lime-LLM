<script setup>
// Segmented range buttons ("Сегодня / Неделя / Месяц").
defineProps({ modelValue: String, options: { type: Array, required: true } });
defineEmits(["update:modelValue"]);
</script>

<template>
  <div class="seg">
    <button v-for="o in options" :key="o.value" type="button" :class="{ on: modelValue === o.value }"
            @click="$emit('update:modelValue', o.value)">{{ o.label }}</button>
  </div>
</template>
