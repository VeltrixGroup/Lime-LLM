<script setup>
// A small live camera tile: latest frame + thin AI boxes (as in the design).
defineProps({
  frame: Object, // { url, meta }
  label: String,
  online: Boolean,
  height: { type: String, default: "96px" },
  boxes: { type: Boolean, default: true },
});
</script>

<template>
  <div class="video" :style="{ height, borderRadius: '10px' }">
    <img v-if="frame" class="frame" :src="frame.url" alt="" />
    <template v-if="frame && boxes">
      <div v-for="p in frame.meta.people" :key="p.id" class="box"
           :style="{ left: p.box[0] * 100 + '%', top: p.box[1] * 100 + '%', width: p.box[2] * 100 + '%', height: p.box[3] * 100 + '%', borderColor: p.color, borderWidth: '1.5px', borderRadius: '3px' }"></div>
    </template>
    <span v-if="!frame && !online" style="position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 12px" class="faint">нет сигнала</span>
    <span v-if="label" class="mono text-2" style="position: absolute; left: 8px; bottom: 6px; font-size: 11px; text-shadow: 0 1px 2px #000">{{ label }}</span>
  </div>
</template>
