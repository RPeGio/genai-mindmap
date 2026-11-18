import { createPinia } from 'pinia';
import { createApp } from 'vue';

import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';

import './styles.css';
import App from './App.vue';
import { useMindMapStore } from './stores/mindmap';

const pinia = createPinia();
const app = createApp(App).use(pinia);
app.mount('#app');

// 进页面先问一句"有没有我还没接住的结果"（上次生成完/被中断、但那棵已经落进画布
// 的树没保存下来）。
//
// 放在入口而不是 `App.vue` 的 `onMounted` 里：这是**一次页面加载一次**的副作用，
// 与界面生命周期无关；挂在组件上会让每个 mount App 的地方（包括界面测试）都多发
// 一次请求，而那些测试断言的是"点了按钮发什么请求"——多出来的一条会把它们全部
// 变成噪声级的失败。失败静默，`probeResumableRun` 内部已经咽掉了。
void useMindMapStore(pinia).probeResumableRun();
