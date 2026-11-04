import { createPinia } from 'pinia';
import { createApp } from 'vue';

import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';

import './styles.css';
import App from './App.vue';

createApp(App).use(createPinia()).mount('#app');
