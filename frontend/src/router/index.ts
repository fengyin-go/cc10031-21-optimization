import { createRouter, createWebHistory } from 'vue-router'

import AnomalyRecoveryView from '@/views/AnomalyRecoveryView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/anomaly-recovery', name: 'anomaly-recovery', component: AnomalyRecoveryView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
