<script setup lang="ts">
import { computed } from 'vue'

import { useLocale } from '@/composables/useLocale'
import { antDesignLocales } from '@/i18n/antDesign'

const { locale } = useLocale()
const antdLocale = computed(() => antDesignLocales[locale.value])
</script>

<template>
  <a-config-provider :locale="antdLocale">
    <router-view />
  </a-config-provider>
</template>
