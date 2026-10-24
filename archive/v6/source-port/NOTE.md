# source-port —— 给上游 PR 用的源码级移植

| 文件 | 对应上游 PR | 内容 |
|---|---|---|
| `plugin-clean.diff` | tencent-connect/openclaw-qqbot **#338** | A~F 六阶段在插件 `src/` 的源码级改动（+245 −12），含上游 `tests/dispatch-deliver.test.ts` 的测试桩更新 |
| `dep.diff` | tencent-connect/qqbot-nodejs **#9** | 平台已定稿的错误分类 `isStreamAlreadySubmittedError()`、`doFlush` 降级、公开 `markFinalized()` / `get finalized()` |

**它不等价于本存档的成品（`../index.cjs.v6`）。** 差别：

- 成品额外含三个早期本地补丁：`messageToolCapabilities`（对应 #339）、
  `isQQBotSilentReply`、`first non-empty partial`
- 成品打在**打包产物**上；这里是**源码级**改动，重建产物会因依赖版本漂移而与成品不同
  （实测同一份源码在 qqbot-nodejs 1.0.4 / 1.0.5 下重建出相差 644 行的产物）

想复现成品请用 `../README.md` 里的路径 A / B / C。
