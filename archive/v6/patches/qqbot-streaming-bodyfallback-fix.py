#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
qqbot streaming body-fallback fix  (2026-10-20 .. 2026-10-24)  --  stages A + B + C + D + E + F
=================================================================
Patch @tencent-connect/openclaw-qqbot dist/index.cjs so that a turn whose final
body was never streamed as a draft still gets delivered (no more "final reply
silently dropped" while channels.qqbot.streaming.mode = "partial").

STAGE A (2026-10-20 00:20, backup index.cjs.bak-bodyfallback-20261020_0020)
--------------------------------------------------------------------------
Root cause (code-confirmed):
  dist/index.cjs  deliverDispatchPayload()  (src/dispatch/dispatch-deliver.ts)
      if (streamOwnsText(state.streamingController)) {
        ... await finalize() ...
        if (!state.streamingController?.shouldFallbackToStatic) {
          return;                 <--  BUG: final body dropped, no send, no error
        }
      }
  streamOwnsText()      == hasStarted && !shouldFallbackToStatic
  shouldFallbackToStatic == isTerminal && sentChunkCount === 0
  => once >=1 draft chunk went out, the final payload returns early: zero API
     call, zero error line (exact 2026-10-19 23:07/23:08 symptom).
  completeSession()'s `if (!this.session) return;` is a secondary contributor.

Stage A fix: (1) cap drafts per turn (QQBOT_STREAM_MAX_DRAFTS=2) so quota is
left for the body; (2) completeSession stops early-returning and, when the
stream did not display the full body, sends it once via the non-streaming path
with a grep-able guard log; (3) the deliver layer injects the final text before
finalize and honours `bodyDelivered`.

STAGE B (2026-10-20 00:52, backup index.cjs.bak-bodyhold-20261020_0052)
----------------------------------------------------------------------
Stage A still failed in the field (2026-10-20 00:46-00:51 log):
  * `[qqbot][guard] draft cap reached ... chars=...` kept climbing (last partial
    text frozen in this.lastAcceptedFull, e.g. 1088 chars);
  * a `new_reply` split cleared this.session; every later partial hit the cap
    branch, so NO new stream opened;
  * end of turn took `phase: streaming -> done (finalize:no_session)` from the
    post-inboundRun safety net (that log line carries NO traceId), i.e.
    deliverDispatchPayload never saw a non-block "final" payload -> setFinalText
    was never called -> this.finalBody stayed "" ;
  * sendFinalBodyIfNeeded() then hit `const body = this.finalBody; if (!body)
    return;` -> SILENT no-op (`final body fallback` count = 0, user got nothing).

Stage B fix:
  B1  body source = longest of {injected finalBody, held lastAcceptedFull};
      if still empty -> WARN `... fallback skipped: no text held (reason=...)`
      instead of a silent return.
  B2  completeSession() final branch pre-sets final text from lastAcceptedFull
      when that is longer (covers "delivery layer never injected").
  B3  deliver layer: setFinalText() is no longer gated on !isTerminal, and an
      already-terminal controller gets one `sendFinalBodyIfNeeded` attempt.
  kept: bodyDelivered only set on a send with no `error`; dedupe
        `body === lastStreamedText`; QQBOT_STREAM_MAX_DRAFTS stays 2.

STAGE C (2026-10-23 21:45, backup index.cjs.bak-streamfix-v3-20261023_2145)
--------------------------------------------------------------------
H1  dedupe whitespace normalization: `body === lastStreamedText` becomes
    normalizeBodyForDedupe(...) (trim + collapse whitespace) -> "differs only
    by whitespace" no longer re-sends.
H2  deliverDispatchPayload: the deliveredTexts dedupe guard is moved ABOVE the
    streaming branch (when no media / not audioAsVoice) so a body already sent
    as a block payload is not sent again as the final body.
㈠   mid-turn block payloads (while streamOwnsText) are now de-duped and SENT
    instead of being silently dropped; budget = 2 per turn (they go through
    sendText -> ReplyLimiter, default limit=4); over budget they are merged and
    held, then folded into the next payload / the final body.
H4  QQBOT_STREAM_MAX_DRAFTS 2 -> 8 (stream frames go through
    sendC2CStreamMessage and never touch ReplyLimiter, so drafts do not eat the
    body's reply quota; the cap only guards against spam / excessive stream
    API calls).
H3  final body pushed back into the stream bubble: completeSession(final) calls
    session.update(full) before session.complete(); lastStreamedText is set to
    full ONLY when both resolved without throwing (then H1 dedupe hits and no
    static re-send happens). Any exception keeps the old lastStreamedText and
    the existing static fallback sends the body -> body never lost.

STAGE D (2026-10-23 22:5x, backup index.cjs.bak-streamfix-v4-<YYYYMMDD_HHMM>) -- v4
--------------------------------------------------------------------------------
唯一症状：长回复出现「气泡=全文 + 末条=全文」两条**逐字节相同**（md5 相同）的消息。
实盘证据（插件回执索引 ref-index.jsonl，A+B+C 生效后 21:56 重启起）：
  * 22:05:21 ROBOT1.0_s74W-...（气泡，流 complete 时记录）984 字
    22:05:24 8590edf8-...（静态 sendText）            984 字  <- 与气泡逐字节相同
  * 22:13:23/27 442/442 字、22:19:37/41 692/692 字 同样逐字节相同
  * 全库 7875 行里只有这 3 对（STREAM->STATIC 归一化相同），且全部在 v3 生效之后
漏点（代码确认）：
  deliverDispatchPayload() 的 `kind === "block"` 分支（v3 的 ㈠）在流接管文本时
  「去重后照发」中段 block 载荷；而 H4 抬高草稿上限后气泡能一路更新到全文，
  于是该 block 的文本 == 气泡当前展示的文本。streamOwnsText() 在 isTerminal 之后
  仍为真（sentChunkCount > 0），因此在「流已收尾、气泡已是全文」之后又静态重发一份。
  静态发送走 deliverReply -> DeliverDebouncer(windowMs=1500) -> 落地比气泡晚 ~3.6s。
  既有判重覆盖不到这条路：H2 只在「非 block」流程顶端判重；block 分支只查
  deliveredTexts，而「气泡展示过的文本」从未记入 deliveredTexts。
修复（三处，均最小）：
  D1  controller 新增只读访问器 displayedBodyText（仅 phase==="done" 时返回
      lastStreamedText，否则空串）——即「气泡已成功展示并收尾的正文」。
  D2  投递层顶部（tool 分支之后、block/最终两条路径之前）新增判重：
      该文本已由气泡成功展示并收尾 => 记入 deliveredTexts 并直接返回。
  D3  block（中段）分支：该 block 文本已等于气泡当前展示文本 => 不静态重发。
底线保持：推流失败 / 流未完成 => displayedBodyText 为空串 => 判重不命中 =>
          既有静态兜底（sendFinalBodyIfNeeded / 降级发送）照旧把正文送达。

STAGE E (2026-10-23 23:5x, backup index.cjs.bak-streamfix-v5-<YYYYMMDD_HHMM>) -- v5
--------------------------------------------------------------------------------
需求（用户实盘反馈）：长回复"流式气泡已推完到全文"之后仍会多收到一条"一次性发出来的
全文"。用户要求【结构上直接不发】，而不是"内容相同时跳过"。

实盘证据（网关日志 AppData\Local\Temp\openclaw\openclaw-2026-10-23.log
+ 插件回执索引 qqbot\data\default\ref-index.jsonl）：
  * 23:44:52.763 stream opened (firstChunk=3)
  * 23:45:14.245 completing stream (sent=8 chars=181 reason=new_reply)   <- 气泡被 new_reply 拆掉
  * 23:45:24.055 phase: streaming -> done (finalize:no_session)          <- 收尾时已无 live session
  * 23:45:24.057 [qqbot][guard] final body fallback send (chars=861 reason=finalize:no_session)
                 streamedChars=171                                       <- 静态末条由这里发出
  * ref-index: 860 字全文出现两条记录（23:45:21.488 / 23:45:24.994）；
    23:41 轮：气泡 1863 全文 + 两条静态分片（1013/847）
调用链（代码确认，均不经过 D2/D3 所在的 deliverDispatchPayload 顶部）：
  deliverDispatchPayload(final payload) -> D2 落空（此刻 phase="streaming"，displayedBodyText=""）->
  streamOwnsText -> setFinalText -> controller.finalize() -> handleFinalize()
     -> this.session === null（已被 new_reply 收掉）-> sentChunkCount>0
     -> transition("done", "finalize:no_session") -> sendFinalBodyIfNeeded() -> 静态 sendText(body)
  （另一条路：kind==="block" 中段载荷在 streamOwnsText 下被 v3 的 ㈠ 静态发出，D3 拿
    冻结的 lastStreamedText 比对，命中不了即重发。）

Stage E 改动（四处，最小替换，锚点唯一）：
  E1  controller 新增 bodyShownInStream 标志（结构状态，非内容比对）。
  E2  该标志仅在「完整正文成功 update 进气泡 + session.complete() 成功」时置 true
      （H3 的 streamShowsFullBody 分支内），即"气泡已收尾且是全文"。
  E3  sendFinalBodyIfNeeded() 顶部：标志为真 => 直接返回、不发静态末条，并打 grep-able 守卫日志
      `[qqbot][guard][E] static final body suppressed ...`。
  E4  kind==="block" 中段载荷在 streamOwnsText 下不再静态发送，改为并入 pendingMidText
      （投递层 C6b 再并入末条正文），并打日志 `[qqbot][guard][E] mid block not sent statically ...`。
  底线：流未启动 / 未收尾 / update 或 complete 抛错 / 气泡未能含全文 => 标志保持 false =>
      静态兜底（sendFinalBodyIfNeeded / 降级发送）照旧送达（A+B 底线不动）。
  引用关系：气泡由 openStream 携带 msg_id=<入站 messageId> 建立（StreamSession.doFlush 传
      msg_id / event_id），本身即挂在用户消息下的被动回复；静态末条被掐掉不丢引用关系；
      ReplyLimiter 只统计静态发送（严格更省额度），本阶段未改动其逻辑。

STAGE F (2026-10-24 00:5x, backup index.cjs.bak-streamfix-v6-<YYYYMMDD_HHMM>) -- v6
--------------------------------------------------------------------------------
症状（用户实盘，网关日志 %TEMP%\openclaw\openclaw-2026-10-24.log）：每批固定 4 条 ERROR，
一天内 3 批（00:18:00 / 00:21:25 / 00:26:59）；10-23 日志里还有 3 批同类（22:09:54 /
23:33:50 / 23:37:53）：
  ① [qqbot:stream] flush failed (state=1): API Error [/v2/users/<openid>/stream_messages]:
         已经提交的消息内容不可修改        <- 核心 StreamSession.doFlush 的 ERROR
  ② [qqbot][guard] final body stream push failed (static fallback will send): 同一个 API Error
                                        <- v3 H3 的 push 失败日志
  ③ [qqbot:stream] flush failed (state=10): 同一个 API Error   <- state=10 = DONE 帧
  ④ complete failed: 同一个 API Error                          <- v5 completeSession 收尾

触发链（两份日志 + 代码双证）：
  a) 某回合先正常推流若干帧（每帧 HTTP 200）。随后生成/回合停顿，同一流的最后一帧到
     finalize 之间的静默间隔 45~92s（10-23: 79.4/72.9/91.5；10-24: 57.9/45.5/68.3）。
  b) finalize 时控制器手里仍是「本地认为存活」（isCompleted=false）的 live session，
     H3 先 session.update(全文)（state=1）-> 平台拒绝：HTTP 404 + biz 文案
     「已经提交的消息内容不可修改」=> ①（核心 ERROR）+ ②（H3 guard ERROR）。
  c) H3 接着 session.complete()（state=10）-> 同一拒绝 => ③（核心 ERROR）+ ④（complete failed）。
  d) sendFinalBodyIfNeeded() 此时 bodyShownInStream=false 且气泡文本（76/95/27/51 字）
     ⊉ 全文（1025/677/…）=> 静态兜底照旧送出全文（正文不丢），但气泡停在半截 => 该轮 2 条。

【推翻用户推断】「new_reply 提前 complete 旧会话后，H3 仍拿旧会话对象去 update/complete」：
  * 代码反证 1：completeSession() 收尾后立即 this.session = null（见 F4 锚点行）；handleChunk 的
    new_reply 分支随后 sendUpdate(text) 会新建 StreamSession —— 没有任何路径复用旧会话对象。
  * 代码反证 2：核心 StreamSession.update() / complete() 开头就是 `if (this.isCompleted) return;`
    —— 若会话真的收尾成功，后续调用根本不会打到 API，②④ 的报错就不可能来自「已收尾的旧会话」。
  * 日志反证：10-23 23:32:29 那一轮是全新回合（`phase: idle → streaming (first_chunk)`），
    全程没有 new_reply 拆分，23:33:50 照样出现同一组 4 条；反之 23:36:08/25/38 连续 3 次
    new_reply 拆分的那一轮全部正常收尾。=> new_reply 既非必要也非充分条件。
  * 结论：报错来自「活着的那个会话」；是平台把这一个流消息定稿（submitted）了，而本地
    isCompleted 仍为 false。「已定稿」是平台侧状态，插件只能从这个精确拒绝里得知。
  * 未确定（如实记录、v6 不依赖）：平台定稿的确切条件。空闲不是充分条件——23:18:08 的 DONE 帧
    在 111s 静默后仍成功、01:24:04 在 45.6s 静默后仍成功。因此 v6 刻意不做任何
    「按空闲时间预判跳过」的启发式（以防误抑制正文）。

Stage F 改动（STAGE_F，九处最小替换，锚点唯一；F0 的 JS 字面量用 \uXXXX 转义，与 bundle 风格一致）：
  F0  顶层新增 isStreamAlreadySubmittedError(err)：精确匹配（biz 文案
      「已经提交的消息内容不可修改」+ stream_messages 路径），
      不做宽泛吞没（不匹配则维持原有 error 行为）。
  F1  controller：新增 streamFinalizedNotified 状态位 + noteFinalizedSession()；
      completeSession() 入口先判 this.session.isCompleted === true（已定稿）=> 跳过 update/complete；
      push/complete 被拒时置该标记（并把会话对象 isCompleted 置真，后续调用在源头 no-op）。
  F2  「已经提交的消息内容不可修改」降级为 warn 且每会话/每回合只记一次：
      核心 doFlush（①）、控制器 sendUpdate（中段）、abort 收尾三处；不再刷 ERROR。
  F3  sendFinalBodyIfNeeded()：抑制判据从「仅 bodyShownInStream」放宽为
      bubbleText = bodyShownInStream ? streamedBodyText : lastStreamedText，仍用 includes 收窄
      => 会话已定稿但气泡已含完整正文时照样抑制静态末条（长回复仍只 1 条）；
      气泡只持更短旧正文时不抑制 => 静态兜底照旧送达（底线不变）。
  F4  new_reply 分支显式重申 this.session = null（拆分后不再对旧会话做任何 update/complete）。
  底线：流未启动 / 未收尾 / 未展示全文 / 任何异常 => 静态兜底（sendFinalBodyIfNeeded）照旧送达。

REPLAY AFTER A PLUGIN UPGRADE / REINSTALL
-----------------------------------------
1. Find the *loaded* copy (contains BOTH "first non-empty partial" and
   "completing stream"):
     Get-ChildItem "$env:USERPROFILE\.openclaw\npm\projects\tencent-connect-openclaw-qqbot-*\node_modules\@tencent-connect\openclaw-qqbot\dist\index.cjs"
   (the big one; a smaller stale copy must NOT be touched)
2. Copy-Item index.cjs index.cjs.bak-bodyhold-<YYYYMMDD_HHMM>
3. python qqbot-streaming-bodyfallback-fix.py "<path to index.cjs>"
   (the script auto-detects which stages are already applied)
4. node --check "<path to index.cjs>"
5. Confirm the 3 historical patch strings still present: messageToolCapabilities,
   isQQBotSilentReply, "first non-empty partial"
6. Restart the gateway (openclaw gateway restart) so the plugin reloads.
"""
import sys

TARGET_DEFAULT = (
    r"C:\Users\zhengzhicheng\.openclaw\npm\projects"
    r"\tencent-connect-openclaw-qqbot-a7ec020d86__openclaw-generation__g-b97d26bd0d7f19d9"
    r"\node_modules\@tencent-connect\openclaw-qqbot\dist\index.cjs"
)

# ======================================================================
# STAGE A
# ======================================================================
STAGE_A = []

# ── A1: draft cap constant ─────────────────────────────────────────────
STAGE_A.append((
    "// src/outbound/streaming-controller.ts\nvar StreamingController = class {",
    "// src/outbound/streaming-controller.ts\n"
    "// \u2500\u2500 2026-10-20 body-fallback guard\uff08\u672c\u5730\u8865\u4e01\uff09\uff1a\u5355\u56de\u5408\u8349\u7a3f\u4e0a\u9650 \u2500\u2500\n"
    "// QQ \u5bf9\u6bcf\u6761\u5165\u7ad9\u6d88\u606f\u7684\u88ab\u52a8\u56de\u590d\u6761\u6570\u6709\u9650\uff08\u7ea6 4\uff09\uff0c\u8349\u7a3f\u5206\u7247\u4f1a\u5403\u6389\u6b63\u6587\u7684\u989d\u5ea6\u3002\n"
    "// \u4e0a\u9650\u8bbe 2\uff1a\u7559\u51fa\u989d\u5ea6\u7ed9\u300c\u6d41\u6536\u5c3e + \u6b63\u6587\u515c\u5e95\u53d1\u9001\u300d\u3002\n"
    "var QQBOT_STREAM_MAX_DRAFTS = 2;\n"
    "var StreamingController = class {",
))

# ── A2: new controller fields ───────────────────────────────────────────
STAGE_A.append((
    "  /** \u4e32\u884c\u961f\u5217 */\n"
    "  chain = Promise.resolve();\n"
    "  // \u2500\u2500 \u516c\u5171\u8bbf\u95ee\u5668 \u2500\u2500",

    "  /** \u4e32\u884c\u961f\u5217 */\n"
    "  chain = Promise.resolve();\n"
    "  /** \u5df2\u771f\u6b63\u63a8\u9001\u5230 QQ \u6d41\u7684\u6700\u540e\u4e00\u6bb5\u6587\u672c\uff08\u8349\u7a3f\u8fbe\u4e0a\u9650\u540e\u4e0d\u518d\u63a8\u9001\uff09 */\n"
    '  lastStreamedText = "";\n'
    "  /** \u56de\u5408\u6700\u7ec8\u6b63\u6587\uff1a\u7531 deliver \u5c42\u5728 finalize \u524d\u6ce8\u5165 */\n"
    '  finalBody = "";\n'
    "  /** \u6b63\u6587\u662f\u5426\u5df2\u7531\u515c\u5e95\u8def\u5f84\u53d1\u51fa\uff08\u9632\u6b62\u4e0e\u9759\u6001\u515c\u5e95\u91cd\u590d\u53d1\u9001\uff09 */\n"
    "  bodyDelivered = false;\n"
    "  // \u2500\u2500 \u516c\u5171\u8bbf\u95ee\u5668 \u2500\u2500",
))

# ── A3: draft cap inside sendUpdate + track lastStreamedText ────────────
STAGE_A.append((
    "  async sendUpdate(text) {\n"
    "    if (!this.session) {\n"
    "      this.session = this.deps.gateway.openStream(this.deps.target, this.deps.replyToId);\n"
    '      this.transition("streaming", "first_chunk");\n'
    "      this.deps.log?.info(`stream opened (firstChunk=${text.length})`);\n"
    "    }\n"
    "    try {\n"
    "      await this.session.update(text);\n"
    "      this.lastAcceptedFull = text;\n"
    "      this.sentChunkCount++;\n"
    "    } catch (err) {",

    "  async sendUpdate(text) {\n"
    "    if (this.sentChunkCount >= QQBOT_STREAM_MAX_DRAFTS) {\n"
    "      this.lastAcceptedFull = text;\n"
    "      this.deps.log?.info(`[qqbot][guard] draft cap reached (max=${QQBOT_STREAM_MAX_DRAFTS}) - holding text for final send (chars=${text.length})`);\n"
    "      return;\n"
    "    }\n"
    "    if (!this.session) {\n"
    "      this.session = this.deps.gateway.openStream(this.deps.target, this.deps.replyToId);\n"
    '      this.transition("streaming", "first_chunk");\n'
    "      this.deps.log?.info(`stream opened (firstChunk=${text.length})`);\n"
    "    }\n"
    "    try {\n"
    "      await this.session.update(text);\n"
    "      this.lastAcceptedFull = text;\n"
    "      this.lastStreamedText = text;\n"
    "      this.sentChunkCount++;\n"
    "    } catch (err) {",
))

# ── A4: completeSession no early-return + final-body fallback ───────────
STAGE_A.append((
    "  async completeSession(reason) {\n"
    "    if (!this.session) return;\n"
    "    this.deps.log?.info(`completing stream (sent=${this.sentChunkCount} chars=${this.lastAcceptedFull.length} reason=${reason ?? \"done\"})`);\n"
    "    try {\n"
    "      await this.session.complete();\n"
    "    } catch (err) {\n"
    "      this.deps.log?.error(`complete failed: ${err instanceof Error ? err.message : String(err)}`);\n"
    "    }\n"
    "    this.session = null;\n"
    "  }",

    "  async completeSession(reason, opts) {\n"
    '    const tag = reason ?? "done";\n'
    "    const isFinal = Boolean(opts?.final);\n"
    "    if (this.session) {\n"
    "      this.deps.log?.info(`completing stream (sent=${this.sentChunkCount} chars=${this.lastAcceptedFull.length} reason=${tag})`);\n"
    "      try {\n"
    "        await this.session.complete();\n"
    "      } catch (err) {\n"
    "        this.deps.log?.error(`complete failed: ${err instanceof Error ? err.message : String(err)}`);\n"
    "      }\n"
    "      this.session = null;\n"
    "    } else if (isFinal) {\n"
    "      this.deps.log?.info(`completing stream (sent=${this.sentChunkCount} chars=${this.lastAcceptedFull.length} reason=${tag}) [no live session - body fallback path]`);\n"
    "    }\n"
    "    if (isFinal) {\n"
    "      await this.sendFinalBodyIfNeeded(tag);\n"
    "    }\n"
    "  }\n"
    "  /** deliver \u5c42\u5728 finalize \u524d\u6ce8\u5165\u56de\u5408\u6700\u7ec8\u6b63\u6587 */\n"
    "  setFinalText(text) {\n"
    '    this.finalBody = typeof text === "string" ? text : "";\n'
    "  }\n"
    "  /** \u6b63\u6587\u515c\u5e95\uff1a\u6d41\u672a\u771f\u6b63\u5c55\u793a\u5b8c\u6574\u6b63\u6587\u65f6\uff0c\u7528\u975e\u6d41\u5f0f\u5355\u6761\u53d1\u9001\u628a\u5b8c\u6574\u6b63\u6587\u8865\u53d1\u51fa\u53bb */\n"
    "  async sendFinalBodyIfNeeded(reason) {\n"
    "    const body = this.finalBody;\n"
    "    if (!body) return;\n"
    "    if (this.bodyDelivered) return;\n"
    "    if (body === this.lastStreamedText) return;\n"
    "    const send = this.deps.sendFallback;\n"
    "    if (!send) {\n"
    "      this.deps.log?.warn(`[qqbot][guard] final body fallback unavailable (no sendFallback) chars=${body.length} reason=${reason}`);\n"
    "      return;\n"
    "    }\n"
    "    this.deps.log?.info(`[qqbot][guard] final body fallback send (chars=${body.length} reason=${reason}) streamedChars=${this.lastStreamedText.length}`);\n"
    "    try {\n"
    "      const result = await send(body);\n"
    "      if (result && result.error) {\n"
    "        this.deps.log?.error(`[qqbot][guard] final body fallback send failed: ${result.error}`);\n"
    "      } else {\n"
    "        this.bodyDelivered = true;\n"
    "      }\n"
    "    } catch (err) {\n"
    "      this.deps.log?.error(`[qqbot][guard] final body fallback send threw: ${err instanceof Error ? err.message : String(err)}`);\n"
    "    }\n"
    "  }",
))

# ── A5: handleFinalize passes final:true and triggers fallback ─────────
STAGE_A.append((
    "  async handleFinalize() {\n"
    "    if (this.isTerminal) return;\n"
    "    if (this.session) {\n"
    "      await this.completeSession();\n"
    '      this.transition("done", "finalize");\n'
    "      this.deps.log?.info(`stream done chunks=${this.sentChunkCount} chars=${this.lastAcceptedFull.length}`);\n"
    "      return;\n"
    "    }\n"
    "    if (this.sentChunkCount > 0) {\n"
    '      this.transition("done", "finalize:no_session");\n'
    "    } else {\n"
    '      this.transition("failed", "finalize:fallback");\n'
    "    }\n"
    "  }",

    "  async handleFinalize() {\n"
    "    if (this.isTerminal) return;\n"
    "    if (this.session) {\n"
    "      await this.completeSession(void 0, { final: true });\n"
    '      this.transition("done", "finalize");\n'
    "      this.deps.log?.info(`stream done chunks=${this.sentChunkCount} chars=${this.lastAcceptedFull.length}`);\n"
    "      return;\n"
    "    }\n"
    "    if (this.sentChunkCount > 0) {\n"
    '      this.transition("done", "finalize:no_session");\n'
    '      await this.sendFinalBodyIfNeeded("finalize:no_session");\n'
    "    } else {\n"
    '      this.transition("failed", "finalize:fallback");\n'
    "    }\n"
    "  }",
))

# ── A6: deliverDispatchPayload - inject final text, honour bodyDelivered
STAGE_A.append((
    "  if (streamOwnsText(state.streamingController)) {\n"
    "    if (state.streamingController && !state.streamingController.isTerminal) {\n"
    "      await state.streamingController.finalize();\n"
    "    }\n"
    "    if (!state.streamingController?.shouldFallbackToStatic) {\n"
    "      return;\n"
    "    }\n"
    "    state.log?.warn(`streaming fallback to static`);\n"
    "  }",

    "  if (state.streamingController?.bodyDelivered) {\n"
    "    if (text) state.deliveredTexts.add(text);\n"
    "    return;\n"
    "  }\n"
    "  if (streamOwnsText(state.streamingController)) {\n"
    "    if (state.streamingController && !state.streamingController.isTerminal) {\n"
    "      state.streamingController.setFinalText(text);\n"
    "      await state.streamingController.finalize();\n"
    "    }\n"
    "    if (!state.streamingController?.shouldFallbackToStatic) {\n"
    "      return;\n"
    "    }\n"
    "    state.log?.warn(`streaming fallback to static`);\n"
    "  }",
))

# ── A7: pass a non-streaming send fallback into the controller ─────────
STAGE_A.append((
    'const streamingController = streamingEnabled ? createStreamingController(envelope, account, log4?.child("streaming")) : null;',
    'const streamingController = streamingEnabled ? createStreamingController(envelope, account, log4?.child("streaming"), (body) => deliverCtx.sendText(deliverCtx.qualifiedTarget, body)) : null;',
))

# ── A8: createStreamingController signature ────────────────────────────
STAGE_A.append((
    "function createStreamingController(envelope, account, log4) {",
    "function createStreamingController(envelope, account, log4, sendFallback) {",
))

# ── A9: createStreamingController deps ─────────────────────────────────
STAGE_A.append((
    "  return new StreamingController({\n"
    "    gateway: gw,\n"
    "    target: {\n"
    '      scope: "c2c",\n'
    "      targetId: envelope.senderId,\n"
    "      msgId: envelope.messageId\n"
    "    },\n"
    "    accountId: account.accountId,\n"
    "    replyToId: envelope.messageId,\n"
    "    log: log4\n"
    "  });\n"
    "}",

    "  return new StreamingController({\n"
    "    gateway: gw,\n"
    "    target: {\n"
    '      scope: "c2c",\n'
    "      targetId: envelope.senderId,\n"
    "      msgId: envelope.messageId\n"
    "    },\n"
    "    accountId: account.accountId,\n"
    "    replyToId: envelope.messageId,\n"
    "    sendFallback,\n"
    "    log: log4\n"
    "  });\n"
    "}",
))

# ======================================================================
# STAGE B  (applied on top of STAGE A)
# ======================================================================
STAGE_B = []

# ── B1: body = longest of {finalBody, lastAcceptedFull}; no silent return ─
STAGE_B.append((
    "  async sendFinalBodyIfNeeded(reason) {\n"
    "    const body = this.finalBody;\n"
    "    if (!body) return;\n"
    "    if (this.bodyDelivered) return;\n"
    "    if (body === this.lastStreamedText) return;",

    "  async sendFinalBodyIfNeeded(reason) {\n"
    "    const injected = this.finalBody || \"\";\n"
    "    const held = this.lastAcceptedFull || \"\";\n"
    "    const body = held.length > injected.length ? held : injected;\n"
    "    if (!body) {\n"
    "      this.deps.log?.warn(`[qqbot][guard] final body fallback skipped: no text held (reason=${reason})`);\n"
    "      return;\n"
    "    }\n"
    "    if (this.bodyDelivered) return;\n"
    "    if (body === this.lastStreamedText) return;",
))

# ── B2: completeSession final branch pre-sets the body from held text ────
STAGE_B.append((
    "    if (isFinal) {\n"
    "      await this.sendFinalBodyIfNeeded(tag);\n"
    "    }",

    "    if (isFinal) {\n"
    "      if (this.lastAcceptedFull.length > this.finalBody.length) {\n"
    "        this.setFinalText(this.lastAcceptedFull);\n"
    "      }\n"
    "      await this.sendFinalBodyIfNeeded(tag);\n"
    "    }",
))

# ── B3: deliver layer - inject even when terminal; one guard attempt ─────
STAGE_B.append((
    "  if (streamOwnsText(state.streamingController)) {\n"
    "    if (state.streamingController && !state.streamingController.isTerminal) {\n"
    "      state.streamingController.setFinalText(text);\n"
    "      await state.streamingController.finalize();\n"
    "    }\n"
    "    if (!state.streamingController?.shouldFallbackToStatic) {\n"
    "      return;\n"
    "    }\n"
    "    state.log?.warn(`streaming fallback to static`);\n"
    "  }",

    "  if (streamOwnsText(state.streamingController)) {\n"
    "    if (state.streamingController) {\n"
    "      state.streamingController.setFinalText(text);\n"
    "    }\n"
    "    if (state.streamingController && !state.streamingController.isTerminal) {\n"
    "      await state.streamingController.finalize();\n"
    "    } else if (state.streamingController) {\n"
    '      await state.streamingController.sendFinalBodyIfNeeded("already_terminal");\n'
    "    }\n"
    "    if (!state.streamingController?.shouldFallbackToStatic) {\n"
    "      return;\n"
    "    }\n"
    "    state.log?.warn(`streaming fallback to static`);\n"
    "  }",
))


# ======================================================================
# STAGE C  (applied on top of STAGE A + STAGE B)  -- 2026-10-23 v3
# ======================================================================
STAGE_C = []

# ── C1: cap 2 -> 8 + new mid-block budget constant (H4 + ㈠) ────────────
STAGE_C.append((
    "// src/outbound/streaming-controller.ts\n"
    "// ── 2026-10-20 body-fallback guard（本地补丁）：单回合草稿上限 ──\n"
    "// QQ 对每条入站消息的被动回复条数有限（约 4），草稿分片会吃掉正文的额度。\n"
    "// 上限设 2：留出额度给「流收尾 + 正文兜底发送」。\n"
    "var QQBOT_STREAM_MAX_DRAFTS = 2;\n"
    "var StreamingController = class {",

    "// src/outbound/streaming-controller.ts\n"
    "// ── 2026-10-20 body-fallback guard（本地补丁）：单回合草稿上限 ──\n"
    "// 2026-10-23 v3（H4）：上限 2 → 8。理据：流帧走 sendC2CStreamMessage\n"
    "// （StreamSession.update/complete），不经过 resolveMsgId/ReplyLimiter，因此草稿不挤占正文额度；\n"
    "// 保留上限仅为防刷屏 / 防过量流 API 调用（6~10 区间取中值 8）。\n"
    "var QQBOT_STREAM_MAX_DRAFTS = 8;\n"
    "// 2026-10-23 v3（㈠）：单回合「中段 block 载荷」预算。中段载荷走 sendText → resolveMsgId\n"
    "// → ReplyLimiter（默认 limit=4，按入站 messageId 计数），每回合最多发 2 条；超出则去重合并保留，\n"
    "// 并入下一条载荷（或末条正文兜底）。\n"
    "var QQBOT_STREAM_MID_BLOCK_MAX = 2;\n"
    "var StreamingController = class {",
))

# ── C2a: H1 whitespace-normalizing helper ──────────────────────────────
STAGE_C.append((
    "function normalizeWs(s) {\n"
    '  return s.replace(/\\s+/g, " ");\n'
    "}",

    "function normalizeWs(s) {\n"
    '  return s.replace(/\\s+/g, " ");\n'
    "}\n"
    "// 2026-10-23 v3（H1）：判重前的空白归一 —— trim + 折叠连续空白/空行，避免「仅差空白」重复发送\n"
    "function normalizeBodyForDedupe(s) {\n"
    '  return String(s ?? "").replace(/\\s+/g, " ").trim();\n'
    "}",
))

# ── C2b: H1 compare via normalized text ────────────────────────────────
STAGE_C.append((
    "    if (body === this.lastStreamedText) return;",
    "    if (normalizeBodyForDedupe(body) === normalizeBodyForDedupe(this.lastStreamedText)) return;",
))

# ── C3: H3 push full body back into the stream bubble (gated) ──────────
STAGE_C.append((
    "    if (this.session) {\n"
    "      this.deps.log?.info(`completing stream (sent=${this.sentChunkCount} chars=${this.lastAcceptedFull.length} reason=${tag})`);\n"
    "      try {\n"
    "        await this.session.complete();\n"
    "      } catch (err) {\n"
    "        this.deps.log?.error(`complete failed: ${err instanceof Error ? err.message : String(err)}`);\n"
    "      }\n"
    "      this.session = null;\n"
    "    } else if (isFinal) {",

    "    if (this.session) {\n"
    "      let streamShowsFullBody = false;\n"
    "      if (isFinal) {\n"
    "        const held = this.lastAcceptedFull || \"\";\n"
    "        const injected = this.finalBody || \"\";\n"
    "        const full = held.length > injected.length ? held : injected;\n"
    "        if (full) {\n"
    "          try {\n"
    "            await this.session.update(full);\n"
    "            this.lastAcceptedFull = full;\n"
    "            streamShowsFullBody = true;\n"
    "            this.deps.log?.info(`[qqbot][guard] final body pushed to stream (chars=${full.length})`);\n"
    "          } catch (err) {\n"
    "            this.deps.log?.error(`[qqbot][guard] final body stream push failed (static fallback will send): ${err instanceof Error ? err.message : String(err)}`);\n"
    "          }\n"
    "        }\n"
    "      }\n"
    "      this.deps.log?.info(`completing stream (sent=${this.sentChunkCount} chars=${this.lastAcceptedFull.length} reason=${tag})`);\n"
    "      try {\n"
    "        await this.session.complete();\n"
    "        if (streamShowsFullBody) {\n"
    "          this.lastStreamedText = this.lastAcceptedFull;\n"
    "        }\n"
    "      } catch (err) {\n"
    "        this.deps.log?.error(`complete failed: ${err instanceof Error ? err.message : String(err)}`);\n"
    "      }\n"
    "      this.session = null;\n"
    "    } else if (isFinal) {",
))

# ── C4: H2 move deliveredTexts dedupe above the streaming branch ───────
STAGE_C.append((
    "  if (state.streamingController?.bodyDelivered) {\n"
    "    if (text) state.deliveredTexts.add(text);\n"
    "    return;\n"
    "  }\n"
    "  if (streamOwnsText(state.streamingController)) {",

    "  if (text && state.deliveredTexts.has(text) && !hasMedia && !payload.audioAsVoice) {\n"
    "    if (streamOwnsText(state.streamingController) && state.streamingController && !state.streamingController.isTerminal) {\n"
    "      state.streamingController.setFinalText(text);\n"
    "      await state.streamingController.finalize();\n"
    "    }\n"
    "    return;\n"
    "  }\n"
    "  if (state.streamingController?.bodyDelivered) {\n"
    "    if (text) state.deliveredTexts.add(text);\n"
    "    return;\n"
    "  }\n"
    "  if (streamOwnsText(state.streamingController)) {",
))

# ── C5: ㈠ mid-turn block payloads: de-duped send + per-turn budget ────
STAGE_C.append((
    "    await forwardMediaUrls(payload, state);\n"
    "    if (streamOwnsText(state.streamingController) || !text || state.deliveredTexts.has(text)) {\n"
    "      return;\n"
    "    }\n"
    "    await sendReply({ text }, info, state.ctx);\n"
    "    state.deliveredTexts.add(text);\n"
    "    return;\n"
    "  }",

    "    await forwardMediaUrls(payload, state);\n"
    "    if (!text || state.deliveredTexts.has(text)) {\n"
    "      return;\n"
    "    }\n"
    "    if (streamOwnsText(state.streamingController)) {\n"
    "      const mid = state.pendingMidText ? `${state.pendingMidText}\\n${text}` : text;\n"
    "      if (state.midBlockSent >= QQBOT_STREAM_MID_BLOCK_MAX) {\n"
    "        state.pendingMidText = mid;\n"
    "        state.log?.info(`[qqbot][guard] mid block budget reached (max=${QQBOT_STREAM_MID_BLOCK_MAX}) - merged and held (chars=${mid.length})`);\n"
    "        return;\n"
    "      }\n"
    "      state.midBlockSent++;\n"
    '      state.pendingMidText = "";\n'
    "      if (!state.deliveredTexts.has(mid)) {\n"
    "        await sendReply({ text: mid }, info, state.ctx);\n"
    "        state.deliveredTexts.add(mid);\n"
    "      }\n"
    "      return;\n"
    "    }\n"
    "    await sendReply({ text }, info, state.ctx);\n"
    "    state.deliveredTexts.add(text);\n"
    "    return;\n"
    "  }",
))

# ── C6a: text must be reassignable (held mid-block folded into body) ───
STAGE_C.append((
    "  const kind = info?.kind;\n"
    '  const text = payload.text?.trim() ?? "";\n'
    "  const hasMedia = !!(payload.mediaUrl || payload.mediaUrls?.length);",

    "  const kind = info?.kind;\n"
    '  let text = payload.text?.trim() ?? "";\n'
    "  const hasMedia = !!(payload.mediaUrl || payload.mediaUrls?.length);",
))

# ── C6b: ㈠ fold held mid-block text into the final body (de-duped) ─────
STAGE_C.append((
    "  if (streamOwnsText(state.streamingController)) {\n"
    "    if (state.streamingController) {\n"
    "      state.streamingController.setFinalText(text);\n"
    "    }",

    "  if (streamOwnsText(state.streamingController)) {\n"
    "    if (state.pendingMidText) {\n"
    "      const heldMid = state.pendingMidText;\n"
    '      state.pendingMidText = "";\n'
    "      if (!text.includes(heldMid)) {\n"
    "        text = `${text}\\n${heldMid}`;\n"
    "        state.log?.info(`[qqbot][guard] folded held mid block into final body (chars=${heldMid.length})`);\n"
    "      }\n"
    "    }\n"
    "    if (state.streamingController) {\n"
    "      state.streamingController.setFinalText(text);\n"
    "    }",
))

# ── C6c: deliverState gains the mid-block budget fields ────────────────
STAGE_C.append((
    "  const deliverState = {\n"
    "    ctx: deliverCtx,\n"
    "    streamingController,\n"
    "    deliveredMediaUrls,\n"
    "    deliveredTexts,\n"
    "    log: dlog,\n"
    "    deliverReply\n"
    "  };",

    "  const deliverState = {\n"
    "    ctx: deliverCtx,\n"
    "    streamingController,\n"
    "    deliveredMediaUrls,\n"
    "    deliveredTexts,\n"
    "    log: dlog,\n"
    "    deliverReply,\n"
    "    midBlockSent: 0,\n"
    '    pendingMidText: ""\n'
    "  };",
))


# ======================================================================
# STAGE D  (applied on top of STAGE A + B + C)  -- 2026-10-23 v4
# 详见文件头 STAGE D 说明。三处改动均为最小替换，锚点唯一。
# ======================================================================
STAGE_D = []

# ── D1: controller 暴露「气泡已展示并收尾的正文」 ──────────────────────
STAGE_D.append((
    "  get shouldFallbackToStatic() {\n"
    "    return this.isTerminal && this.sentChunkCount === 0;\n"
    "  }",

    "  get shouldFallbackToStatic() {\n"
    "    return this.isTerminal && this.sentChunkCount === 0;\n"
    "  }\n"
    "  /** 2026-10-23 v4：气泡\u5df2\u300c\u6210\u529f\u5c55\u793a\u5e76\u6536\u5c3e\u300d\u7684\u6b63\u6587\uff1b\u672a\u6210\u529f\u5c55\u793a\u65f6\u4e3a\u7a7a\u4e32\u3002\n"
    "   *  \u6295\u9012\u5c42\u636e\u6b64\u5224\u91cd\uff1a\u6c14\u6ce1\u5df2\u542b\u8be5\u6587\u672c\u65f6\uff0c\u4efb\u4f55\u9759\u6001\u91cd\u53d1\u90fd\u5e94\u8df3\u8fc7\uff08\u6b63\u6587\u4e0d\u4e22\uff09\u3002 */\n"
    "  get displayedBodyText() {\n"
    '    return this.phase === "done" ? this.lastStreamedText || "" : "";\n'
    "  }",
))

# ── D2: 投递层顶部判重（气泡已展示 => 任何静态路径不再重发） ────────────
STAGE_D.append((
    '  if (kind === "tool") {\n'
    "    await forwardMediaUrls(payload, state);\n"
    "    return;\n"
    "  }\n"
    '  if (kind === "block") {',

    '  if (kind === "tool") {\n'
    "    await forwardMediaUrls(payload, state);\n"
    "    return;\n"
    "  }\n"
    "  // 2026-10-23 v4\uff08\u6f0f\u70b9\u4fee\u590d\uff09\uff1a\u6c14\u6ce1\u5df2\u6210\u529f\u5c55\u793a\u8be5\u6587\u672c\u4e14\u6d41\u5df2\u6536\u5c3e \u21d2 \u4efb\u4f55\u9759\u6001\u8def\u5f84\u90fd\u4e0d\u518d\u91cd\u53d1\u3002\n"
    "  // \u63a8\u6d41\u5931\u8d25 / \u6d41\u672a\u5b8c\u6210 \u21d2 displayedBodyText \u4e3a\u7a7a\u4e32 \u21d2 \u4e0d\u547d\u4e2d\uff0c\u9759\u6001\u515c\u5e95\u7167\u65e7\u9001\u8fbe\u3002\n"
    "  if (text && !hasMedia && !payload.audioAsVoice && state.streamingController && normalizeBodyForDedupe(text) === normalizeBodyForDedupe(state.streamingController.displayedBodyText)) {\n"
    "    state.deliveredTexts.add(text);\n"
    "    state.log?.info(`[qqbot][guard] text already shown in stream bubble - static re-send skipped (chars=${text.length} kind=${kind ?? \"none\"})`);\n"
    "    return;\n"
    "  }\n"
    '  if (kind === "block") {',
))

# ── D3: block（中段）分支：文本已在气泡里 => 不静态重发 ─────────────────
STAGE_D.append((
    "      const mid = state.pendingMidText ? `${state.pendingMidText}\\n${text}` : text;\n"
    "      if (state.midBlockSent >= QQBOT_STREAM_MID_BLOCK_MAX) {",

    "      const mid = state.pendingMidText ? `${state.pendingMidText}\\n${text}` : text;\n"
    "      if (state.streamingController && normalizeBodyForDedupe(mid) === normalizeBodyForDedupe(state.streamingController.lastStreamedText)) {\n"
    "        state.deliveredTexts.add(mid);\n"
    "        state.log?.info(`[qqbot][guard] mid block already shown in stream bubble - static re-send skipped (chars=${mid.length})`);\n"
    "        return;\n"
    "      }\n"
    "      if (state.midBlockSent >= QQBOT_STREAM_MID_BLOCK_MAX) {",
))


# ======================================================================
# STAGE E  (applied on top of STAGE A + B + C + D)  -- 2026-10-23 v5
# 结构上不再发"一次性全文"静态末条：仅当气泡已成功收尾且含完整正文时抑制。
# ======================================================================
STAGE_E = []

# ── E1: controller 新增结构标志 bodyShownInStream ──────────────────────
STAGE_E.append((
    r"""  /** 正文是否已由兜底路径发出（防止与静态兜底重复发送） */
  bodyDelivered = false;
""",
    r"""  /** 正文是否已由兜底路径发出（防止与静态兜底重复发送） */
  bodyDelivered = false;
  /** 2026-10-23 v5（STAGE E）：完整正文已成功推入气泡并收尾（气泡即全文 ⇒ 静态末条不再发） */
  bodyShownInStream = false;
  /** 2026-10-23 v5（STAGE E）：上次成功推入并收尾的正文文本（用于确认气泡确实含完整正文） */
  streamedBodyText = "";
""",
))

# ── E2: 只在「全文 update 入气泡 + complete 成功」时置位 ───────────────
STAGE_E.append((
    r"""      try {
        await this.session.complete();
        if (streamShowsFullBody) {
          this.lastStreamedText = this.lastAcceptedFull;
        }
      } catch (err) {
""",
    r"""      try {
        await this.session.complete();
        if (streamShowsFullBody) {
          this.lastStreamedText = this.lastAcceptedFull;
          this.streamedBodyText = this.lastAcceptedFull;
          this.bodyShownInStream = true;
        }
      } catch (err) {
""",
))

# ── E3: sendFinalBodyIfNeeded 顶部结构化抑制（带"不得丢正文"收窄） ─────
STAGE_E.append((
    r"""    if (this.bodyDelivered) return;
    if (normalizeBodyForDedupe(body) === normalizeBodyForDedupe(this.lastStreamedText)) return;
""",
    r"""    if (this.bodyShownInStream && normalizeBodyForDedupe(this.streamedBodyText).includes(normalizeBodyForDedupe(body))) {
      this.deps.log?.info(`[qqbot][guard][E] static final body suppressed - stream bubble already holds the full body (chars=${body.length} reason=${reason})`);
      return;
    }
    if (this.bodyDelivered) return;
    if (normalizeBodyForDedupe(body) === normalizeBodyForDedupe(this.lastStreamedText)) return;
""",
))

# ── E4: 中段 block 在流接管正文时不再静态发送，改为并入末条正文 ────────
STAGE_E.append((
    r"""    if (streamOwnsText(state.streamingController)) {
      const mid = state.pendingMidText ? `${state.pendingMidText}\n${text}` : text;
      if (state.streamingController && normalizeBodyForDedupe(mid) === normalizeBodyForDedupe(state.streamingController.lastStreamedText)) {
        state.deliveredTexts.add(mid);
        state.log?.info(`[qqbot][guard] mid block already shown in stream bubble - static re-send skipped (chars=${mid.length})`);
        return;
      }
      if (state.midBlockSent >= QQBOT_STREAM_MID_BLOCK_MAX) {
        state.pendingMidText = mid;
        state.log?.info(`[qqbot][guard] mid block budget reached (max=${QQBOT_STREAM_MID_BLOCK_MAX}) - merged and held (chars=${mid.length})`);
        return;
      }
      state.midBlockSent++;
      state.pendingMidText = "";
      if (!state.deliveredTexts.has(mid)) {
        await sendReply({ text: mid }, info, state.ctx);
        state.deliveredTexts.add(mid);
      }
      return;
    }
""",
    r"""    if (streamOwnsText(state.streamingController)) {
      const mid = state.pendingMidText ? `${state.pendingMidText}\n${text}` : text;
      // 2026-10-23 v5（STAGE E）：流已接管正文 ⇒ 中段 block 结构上不再静态发送（气泡就是交付通道）。
      // 文本保留在 pendingMidText：投递层随后并入末条正文（H3 推入气泡；气泡不可用时由兜底静态发送兜住）。
      state.pendingMidText = mid;
      state.log?.info(`[qqbot][guard][E] mid block not sent statically - stream owns text (chars=${mid.length} alreadyShown=${Boolean(state.streamingController && normalizeBodyForDedupe(mid) === normalizeBodyForDedupe(state.streamingController.lastStreamedText))})`);
      return;
    }
""",
))


# ======================================================================
# STAGE F  (applied on top of STAGE A + B + C + D + E)  -- 2026-10-24 v6
# 已定稿流：会话状态判定（F1）+ 精确降级不刷 error（F2）+ 结构抑制静态末条（F3）+ 拆分不引用旧会话（F4）。
# ======================================================================
_HINT_CN = "已经提交的消息内容不可修改"
_HINT_ESC = "".join("\\u%04X" % ord(ch) for ch in _HINT_CN)

STAGE_F = []

# ── F0: 顶层共享助手：精确判定「平台已定稿该流消息」 ─────────────────────
STAGE_F.append((
    r"""// node_modules/@tencent-connect/qqbot-nodejs/dist/protocol/utils/format.js
function formatErrorMessage(err) {
""",
    r"""// 2026-10-24 v6（STAGE F）：「已经提交的消息内容不可修改」= 平台已把该流消息定稿（submitted），
// 之后任何 REPLACE 改写都会被拒（HTTP 404 + biz 文案）。这是可预期状态，不是插件故障。
// 精确匹配（该 biz 文案 + stream_messages 路径）后降级为 warn；据此把会话标记为已定稿。
function isStreamAlreadySubmittedError(err) {
  if (!err) return false;
  const text = `${err.bizMessage ?? ""} ${err.message ?? ""} ${err.cause?.message ?? ""}`;
  if (!text.includes("@@HINT@@")) return false;
  if (text.includes("stream_messages")) return true;
  return typeof err.path === "string" && err.path.includes("stream_messages");
}

// node_modules/@tencent-connect/qqbot-nodejs/dist/protocol/utils/format.js
function formatErrorMessage(err) {
""".replace("@@HINT@@", _HINT_ESC),
))

# ── F1a: controller 新增「平台已定稿」状态位 ─────────────────────────
STAGE_F.append((
    r"""  /** 2026-10-23 v5（STAGE E）：上次成功推入并收尾的正文文本（用于确认气泡确实含完整正文） */
  streamedBodyText = "";
""",
    r"""  /** 2026-10-23 v5（STAGE E）：上次成功推入并收尾的正文文本（用于确认气泡确实含完整正文） */
  streamedBodyText = "";
  /** 2026-10-24 v6（STAGE F）：已就「平台已定稿该流消息」告警过一次（每回合只记一次 warn） */
  streamFinalizedNotified = false;
""",
))

# ── F1b: controller 新增 noteFinalizedSession() ─────────────────────
STAGE_F.append((
    r"""  /** deliver 层在 finalize 前注入回合最终正文 */
  setFinalText(text) {
""",
    r"""  /** 2026-10-24 v6（STAGE F）：记为「平台已定稿该流消息」——把会话标记为已收尾 + 每回合只记一次 warn */
  noteFinalizedSession(detail) {
    if (this.session) {
      this.session.isCompleted = true;
    }
    if (this.streamFinalizedNotified) return;
    this.streamFinalizedNotified = true;
    this.deps.log?.warn(`[qqbot][guard][F] stream message already submitted on platform - session finalized, static fallback path expected (${detail})`);
  }
  /** deliver 层在 finalize 前注入回合最终正文 */
  setFinalText(text) {
""",
))

# ── F1c: completeSession 入口先判会话状态（已定稿 => 不再 update/complete） ──
STAGE_F.append((
    r"""    if (this.session) {
      let streamShowsFullBody = false;
      if (isFinal) {
""",
    r"""    if (this.session) {
      let streamShowsFullBody = false;
      // 2026-10-24 v6（STAGE F/F1）：平台已定稿该会话（isCompleted，或本次 update 被拒）=> 不再 update/complete
      let sessionFinalized = this.session.isCompleted === true;
      if (sessionFinalized) {
        this.noteFinalizedSession(`reason=${tag} source=session_state`);
      }
      if (isFinal && !sessionFinalized) {
""",
))

# ── F1d: 推流被拒（②）=> 记可预期状态，不再刷 error ─────────────────────
STAGE_F.append((
    r"""          } catch (err) {
            this.deps.log?.error(`[qqbot][guard] final body stream push failed (static fallback will send): ${err instanceof Error ? err.message : String(err)}`);
          }
""",
    r"""          } catch (err) {
            if (isStreamAlreadySubmittedError(err)) {
              sessionFinalized = true;
              this.noteFinalizedSession(`reason=${tag} chars=${full.length}`);
            } else {
              this.deps.log?.error(`[qqbot][guard] final body stream push failed (static fallback will send): ${err instanceof Error ? err.message : String(err)}`);
            }
          }
""",
))

# ── F1e: 已定稿则跳过 complete()（③④不再产生）；其余路径逐字不变 ────────
STAGE_F.append((
    r"""      this.deps.log?.info(`completing stream (sent=${this.sentChunkCount} chars=${this.lastAcceptedFull.length} reason=${tag})`);
      try {
        await this.session.complete();
        if (streamShowsFullBody) {
          this.lastStreamedText = this.lastAcceptedFull;
          this.streamedBodyText = this.lastAcceptedFull;
          this.bodyShownInStream = true;
        }
      } catch (err) {
        this.deps.log?.error(`complete failed: ${err instanceof Error ? err.message : String(err)}`);
      }
""",
    r"""      if (!sessionFinalized) {
        this.deps.log?.info(`completing stream (sent=${this.sentChunkCount} chars=${this.lastAcceptedFull.length} reason=${tag})`);
        try {
          await this.session.complete();
          if (streamShowsFullBody) {
            this.lastStreamedText = this.lastAcceptedFull;
            this.streamedBodyText = this.lastAcceptedFull;
            this.bodyShownInStream = true;
          }
        } catch (err) {
          if (isStreamAlreadySubmittedError(err)) {
            this.noteFinalizedSession(`reason=${tag} stage=complete`);
          } else {
            this.deps.log?.error(`complete failed: ${err instanceof Error ? err.message : String(err)}`);
          }
        }
      }
""",
))

# ── F2a: 核心 StreamSession.doFlush（①）——只记一次 warn 并标记已定稿 ────
STAGE_F.append((
    r"""    } catch (err) {
      flushFailed = true;
      this.opts.logger?.error?.(`[qqbot:stream] flush failed (state=${state}): ${formatErrorMessage(err)}`);
      throw err;
""",
    r"""    } catch (err) {
      flushFailed = true;
      if (isStreamAlreadySubmittedError(err)) {
        this.isCompleted = true;
        if (!this._alreadySubmittedNotified) {
          this._alreadySubmittedNotified = true;
          this.opts.logger?.warn?.(`[qqbot:stream] stream already submitted on platform - session finalized, later frames skipped (state=${state})`);
        }
      } else {
        this.opts.logger?.error?.(`[qqbot:stream] flush failed (state=${state}): ${formatErrorMessage(err)}`);
      }
      throw err;
""",
))

# ── F2b: 中段 update 被拒（同类）不再刷 error（控制流逐字不变） ───────────
STAGE_F.append((
    r"""    } catch (err) {
      this.deps.log?.error(`update failed (len=${text.length}): ${err instanceof Error ? err.message : String(err)}`);
      this.session = null;
      this.transition("failed", "update_error");
    }
""",
    r"""    } catch (err) {
      if (isStreamAlreadySubmittedError(err)) {
        this.deps.log?.warn(`[qqbot][guard][F] stream update rejected - platform already finalized the stream message (len=${text.length})`);
      } else {
        this.deps.log?.error(`update failed (len=${text.length}): ${err instanceof Error ? err.message : String(err)}`);
      }
      this.session = null;
      this.transition("failed", "update_error");
    }
""",
))

# ── F2c: abort 收尾被拒（同类）不再刷 error ────────────────────────────
STAGE_F.append((
    r"""      } catch (e) {
        this.deps.log?.error(`abort complete failed: ${e instanceof Error ? e.message : String(e)}`);
      }
""",
    r"""      } catch (e) {
        if (isStreamAlreadySubmittedError(e)) {
          this.deps.log?.warn(`[qqbot][guard][F] abort complete skipped - platform already finalized the stream message`);
        } else {
          this.deps.log?.error(`abort complete failed: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
""",
))

# ── F3: 即使会话已定稿，气泡已含完整正文 => 仍抑制静态末条 ──────────────
STAGE_F.append((
    r"""    if (this.bodyShownInStream && normalizeBodyForDedupe(this.streamedBodyText).includes(normalizeBodyForDedupe(body))) {
      this.deps.log?.info(`[qqbot][guard][E] static final body suppressed - stream bubble already holds the full body (chars=${body.length} reason=${reason})`);
      return;
    }
""",
    r"""    // 2026-10-24 v6（STAGE F/F3）：结构判据优先（bodyShownInStream = 完整正文成功 update + complete 成功）；
    // 会话被平台定稿 / 推流被拒时退回「最后一次真正推入气泡的文本」(lastStreamedText)。
    // 两路都用 includes 收窄：气泡只持更短旧正文时不抑制 => 静态兜底照旧把正文送达（底线不变）。
    const bubbleText = this.bodyShownInStream ? this.streamedBodyText : this.lastStreamedText || "";
    const normalizedBubble = normalizeBodyForDedupe(bubbleText);
    if (normalizedBubble && normalizedBubble.includes(normalizeBodyForDedupe(body))) {
      this.deps.log?.info(`[qqbot][guard][F] static final body suppressed - stream bubble already holds the full body (chars=${body.length} reason=${reason} source=${this.bodyShownInStream ? "finalize" : "lastStreamed"})`);
      return;
    }
""",
))

# ── F4: new_reply 拆分后显式重申「不再对旧会话做任何 update/complete」 ──
STAGE_F.append((
    r"""      this.deps.log?.info(`new reply: lastAccepted=${this.lastAcceptedFull.length}\u2192chunk=${text.length}`);
      await this.completeSession("new_reply");
      this.lastAcceptedFull = "";
""",
    r"""      this.deps.log?.info(`new reply: lastAccepted=${this.lastAcceptedFull.length}\u2192chunk=${text.length}`);
      await this.completeSession("new_reply");
      // 2026-10-24 v6（STAGE F/F4）：拆分后不再对旧会话做任何 update/complete。
      // completeSession("new_reply") 内部已把旧会话收尾并置空；此处显式重申该不变量
      // （源码确认没有其它持有旧会话引用的路径），随后 sendUpdate 才会开新会话。
      this.session = null;
      this.lastAcceptedFull = "";
""",
))


def apply_stage(txt, stage, label):
    errors = []
    for i, (old, new) in enumerate(stage, 1):
        n = txt.count(old)
        if n != 1:
            errors.append("%s-%d: expected exactly 1 occurrence, found %d" % (label, i, n))
        if new and new in txt:
            errors.append("%s-%d: new text already present" % (label, i))
    if errors:
        return None, errors
    for i, (old, new) in enumerate(stage, 1):
        txt = txt.replace(old, new, 1)
        print("applied %s-%d" % (label, i))
    return txt, []


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else TARGET_DEFAULT
    txt = open(path, "rb").read().decode("utf-8")

    for marker in ("first non-empty partial", "completing stream"):
        if marker not in txt:
            print("ABORT: not the loaded copy (missing %r)" % marker)
            return 2

    has_a = "QQBOT_STREAM_MAX_DRAFTS" in txt
    has_b = "final body fallback skipped: no text held" in txt
    has_c = "QQBOT_STREAM_MID_BLOCK_MAX" in txt
    has_d = "displayedBodyText" in txt
    has_e = "bodyShownInStream" in txt
    has_f = "isStreamAlreadySubmittedError" in txt

    if has_c and has_d and has_e and has_f:
        print("already fully patched (stages A+B+C+D+E+F) - nothing to do")
        return 0

    if has_d and not has_c:
        print("ABORT: stage D present but stage C missing - unexpected file state")
        return 4

    if not has_a:
        txt, err = apply_stage(txt, STAGE_A, "A")
        if err:
            print("ABORT (nothing written):")
            for e in err:
                print("  -", e)
            return 3
    else:
        print("stage A already present - skipping stage A")

    if not has_b:
        txt, err = apply_stage(txt, STAGE_B, "B")
        if err:
            print("ABORT (nothing written):")
            for e in err:
                print("  -", e)
            return 3
    else:
        print("stage B already present - skipping stage B")

    if not has_c:
        txt, err = apply_stage(txt, STAGE_C, "C")
        if err:
            print("ABORT (nothing written):")
            for e in err:
                print("  -", e)
            return 3
    else:
        print("stage C already present - skipping stage C")

    if not has_d:
        txt, err = apply_stage(txt, STAGE_D, "D")
        if err:
            print("ABORT (nothing written):")
            for e in err:
                print("  -", e)
            return 3
    else:
        print("stage D already present - skipping stage D")

    if not has_e:
        txt, err = apply_stage(txt, STAGE_E, "E")
        if err:
            print("ABORT (nothing written):")
            for e in err:
                print("  -", e)
            return 3
    else:
        print("stage E already present - skipping stage E")

    if not has_f:
        txt, err = apply_stage(txt, STAGE_F, "F")
        if err:
            print("ABORT (nothing written):")
            for e in err:
                print("  -", e)
            return 3
    else:
        print("stage F already present - skipping stage F")

    open(path, "wb").write(txt.encode("utf-8"))
    print("WROTE", path, "bytes=", len(txt.encode("utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())