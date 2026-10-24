# archive/v6 —— 我们当前在跑的 qqbot 插件 v6 存档

> 存档时间：2026-10-24
> 适用插件：`@tencent-connect/openclaw-qqbot` **2.0.4**
> 存档产物 sha256：**`ed4ec421b418d7ef15f666b08c597951bde1483dac6690a9cb742a1e154b1d99`**（564,064 B）

## 这是什么

在我们给上游提 PR 之前，OpenClaw 实例上的 qqbot 插件 2.0.4 有六个同源毛病：

| # | 症状 |
|---|---|
| ① | 流式（`streaming.mode=partial`）下**最终正文被静默丢弃** —— 零 API 调用、零 error 日志，用户只看到半截草稿 |
| ② | 短回复被发两次（气泡 + 静态兜底） |
| ③ | 气泡停在最早一片（草稿封顶后不再更新） |
| ④ | 长回复出现「气泡=全文 + 末条=全文」两条完全相同 |
| ⑤ | ④的残留：静态末条必须**结构上不发** |
| ⑥ | 每批回复刷 4 条 ERROR（平台已定稿的流被反复 REPLACE 拒绝） |

这份存档保存我们当时**实际运行的最终产物**，以及**从 npm 纯净原版完整复现它的全部材料**。

对应的源码级 PR 已提交上游（见文末），上游合并发版后即可撤下本地补丁。

## 目录内容

```
archive/v6/
├─ README.md                       ← 本文件
├─ index.cjs.v6                    ← 我们当前在跑的成品（564,064 B）
├─ sha256sums.txt                  ← 本目录全部文件的 sha256（权威校验清单）
├─ base/
│  ├─ stock-2.0.4-index.cjs         ← 补丁基线（552,290 B，含三个早期本地补丁）
│  ├─ npm-original-2.0.4-index.cjs  ← npm 上的纯净原版（549,655 B）
│  └─ historical-patches.diff      ← 上面两者的差（那三个早期补丁，2,635 B）
├─ patches/
│  ├─ qqbot-streaming-bodyfallback-fix.py     ← A~F 六阶段重放脚本（幂等，可重复执行）
│  └─ qqbot-streaming-bodyfallback-fix.patch  ← 同上，diff 形态
├─ verify/
│  └─ qqbot-parity-check.py        ← 一致性校验器（比 size + sha256 + 阶段锚点 + 行尾 + 语法）
└─ source-port/                    ← 源码级移植（对应上游 PR，非本存档必需）
   ├─ plugin-clean.diff
   ├─ dep.diff
   └─ NOTE.md
```

## 怎么复现（两条路径都已验证**逐字节一致**）

### 路径 A：直接用成品（最快）

把 `index.cjs.v6` 覆盖到插件的 dist 目录：

```bash
D=~/.openclaw/npm/projects/tencent-connect-openclaw-qqbot-*/node_modules/@tencent-connect/openclaw-qqbot/dist
cp "$D/index.cjs" "$D/index.cjs.bak-$(date +%Y%m%d_%H%M%S)"   # 先备份
cp archive/v6/index.cjs.v6 "$D/index.cjs"
node --check "$D/index.cjs" && echo "语法 OK"
```

### 路径 B：从 npm 纯净原版完整复现（验证可重复性）

```bash
mkdir -p work/node_modules/@tencent-connect/openclaw-qqbot/dist && cd work
cp archive/v6/base/npm-original-2.0.4-index.cjs node_modules/@tencent-connect/openclaw-qqbot/dist/index.cjs

# ① 打三个早期本地补丁（得到补丁基线 552,290 B）
patch -p0 node_modules/@tencent-connect/openclaw-qqbot/dist/index.cjs \
      < archive/v6/historical-patches.diff

# ② 打 A~F 六阶段补丁（得到成品 564,064 B）
git -c core.autocrlf=false -c core.eol=lf apply \
    archive/v6/patches/qqbot-streaming-bodyfallback-fix.patch

# ③ 校验
python3 archive/v6/verify/qqbot-parity-check.py \
        node_modules/@tencent-connect/openclaw-qqbot/dist/index.cjs
```

预期结果：

```
sha256 : ed4ec421b418d7ef15f666b08c597951bde1483dac6690a9cb742a1e154b1d99
⇒ 与主人环境 **逐字节一致（PASS）** ✓
```

> 若跳过第 ① 步（直接从 npm 原版打 A~F），产物是 561,429 B —— 少了那三个早期补丁，
> **校验器会判 FAIL**，这是预期行为。

### 路径 C：从补丁基线直接重放

跳过第 ① 步，直接用 `base/stock-2.0.4-index.cjs` 起（它是 552,290 B 的基线），
后面步骤相同，结果同样是 564,064 B。

## 校验

```bash
sha256sum -c archive/v6/sha256sums.txt
python3 archive/v6/verify/qqbot-parity-check.py <你的 index.cjs>
```

校验器会检查：字节数、sha256、六个阶段的锚点是否齐备、行尾是否为 LF、`node --check` 是否通过。

## 怎么撤

```bash
# 1) 用备份还原
cp "$D/index.cjs.bak-<时间戳>" "$D/index.cjs"
# 2) 或直接重装官方版本
npm i @tencent-connect/openclaw-qqbot@2.0.4
```

改完记得**重启网关**（插件代码在启动时加载）。

## 与上游 PR 的对应关系

| 覆盖的毛病 | 上游 PR | 仓库 |
|---|---|---|
| ① ② ③ ④ ⑤（流式投递主修） | **#338** | tencent-connect/openclaw-qqbot |
| ⑥（平台已定稿不再刷 ERROR）+ 会话定稿公开 API | **#9** | tencent-connect/qqbot-nodejs |
| 群聊 message 工具声明 | **#339** | tencent-connect/openclaw-qqbot |

`source-port/` 里是给这两个 PR 用的源码级 diff（A~F 与依赖包侧），**它不等价于本存档的成品**：
成品额外包含三个早期本地补丁（`messageToolCapabilities` / `isQQBotSilentReply` /
`first non-empty partial`），那三个不在上游 PR 范围内 —— 其中 `messageToolCapabilities`
对应 #339，另两个是更早的本地改动。

## 两个坑（都是实测踩到的）

1. **行尾**：应用 `.patch` 时必须带 `-c core.autocrlf=false -c core.eol=lf`，
   否则每行被写成 CRLF，sha 对不上。
2. **依赖漂移**：上游 `package.json` 用 `^1.0.4` 范围且**没有提交 lockfile**，
   所以「用仓库源码重建出来的产物」会随依赖版本变化 —— 我们实测到同一份源码在
   `qqbot-nodejs` 1.0.4 与 1.0.5 下重建出**相差 644 行**的产物。
   本存档记录的是**已发布产物 + 文本补丁**，与依赖版本无关，故可稳定复现。
