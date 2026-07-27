<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useLocale } from '@/composables/useLocale'
import { antDesignLocales } from '@/i18n/antDesign'
import FirstLoginLocaleGate from '@/components/FirstLoginLocaleGate.vue'

const { locale } = useLocale()
const route = useRoute()
const antdLocale = computed(() => antDesignLocales[locale.value])

// The hosted first-login locale gate is withheld on `meta.public` routes
// (login / setup / OAuth callbacks) — blocking there would deadlock the
// very flow that produces the identity the gate reads. `route.name` is
// empty only on the initial START_LOCATION, before the first navigation
// resolves; treating that as public avoids a one-frame flash of the
// overlay over the login screen. Self-host renders nothing either way.
const publicRoute = computed(() => !route.name || Boolean(route.meta?.public))
</script>

<template>
  <a-config-provider :locale="antdLocale">
    <router-view />
    <FirstLoginLocaleGate :public-route="publicRoute" />
  </a-config-provider>
</template>
