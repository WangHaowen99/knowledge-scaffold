// node 跑单测时的模块解析（ADR-0055）：官方包界面代码在 assets/packs/<包>/ui/，和 vite.config.js 同一条规则——
// @onto/ 指平台前端 src/，@packs/ 指包目录，包代码里的裸模块名按平台前端的依赖解析。
import { register } from 'node:module';

register('./pack-resolve-hooks.mjs', import.meta.url);
