/**
 * 静默回复识别 + delta 部分正文 的回归测试。
 *
 * 运行方式: npx tsx tests/silent-reply.test.ts
 */
import assert from 'node:assert/strict';
import { isQQBotSilentReply } from '../src/utils/silent-reply.js';
import { createQqbotReplyOptions } from '../src/dispatch/reply-options.js';

let passed = 0;
let failed = 0;
const failedTests: string[] = [];

async function test(name: string, fn: () => void | Promise<void>) {
  try {
    await fn();
    console.log(`  \u2713 ${name}`);
    passed++;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error(`  \u2717 ${name}\n    ${msg}`);
    failed++;
    failedTests.push(name);
  }
}

await test('静默 token：NO_REPLY / HEARTBEAT_OK 精确匹配', () => {
  assert.equal(isQQBotSilentReply('NO_REPLY'), true);
  assert.equal(isQQBotSilentReply('no_reply'), true);
  assert.equal(isQQBotSilentReply('  NO_REPLY  '), true);
  assert.equal(isQQBotSilentReply('HEARTBEAT_OK'), true);
  assert.equal(isQQBotSilentReply('NO_REPLY NO_REPLY'), true);
});

await test('静默 token：前缀匹配（全大写、长度>=2）', () => {
  assert.equal(isQQBotSilentReply('NO'), true);
  assert.equal(isQQBotSilentReply('NO_'), true);
  assert.equal(isQQBotSilentReply('HE'), true);
  assert.equal(isQQBotSilentReply('N'), false);
  assert.equal(isQQBotSilentReply('No'), false);
});

await test('静默 token：普通回复不被误判', () => {
  assert.equal(isQQBotSilentReply('你好'), false);
  assert.equal(isQQBotSilentReply('NO_REPLY please'), false);
  assert.equal(isQQBotSilentReply(''), false);
  assert.equal(isQQBotSilentReply(undefined), false);
  assert.equal(isQQBotSilentReply(null), false);
});

await test('onPartialReply：只有 delta 时也要把正文交给流', async () => {
  const got: string[] = [];
  const options = createQqbotReplyOptions({
    streamingController: {
      onPartialReply: async (text: string) => {
        got.push(text);
      },
    } as never,
  });
  const handler = options.onPartialReply as (p: { text?: string; delta?: string }) => Promise<boolean>;
  const r = await handler({ delta: 'delta-only' });
  assert.equal(r, true);
  assert.deepEqual(got, ['delta-only']);
});

await test('onPartialReply：text 优先于 delta', async () => {
  const got: string[] = [];
  const options = createQqbotReplyOptions({
    streamingController: {
      onPartialReply: async (text: string) => {
        got.push(text);
      },
    } as never,
  });
  const handler = options.onPartialReply as (p: { text?: string; delta?: string }) => Promise<boolean>;
  const r = await handler({ text: 'from-text', delta: 'from-delta' });
  assert.equal(r, true);
  assert.deepEqual(got, ['from-text']);
});

await test('onPartialReply：空正文返回 false', async () => {
  const got: string[] = [];
  const options = createQqbotReplyOptions({
    streamingController: {
      onPartialReply: async (text: string) => {
        got.push(text);
      },
    } as never,
  });
  const handler = options.onPartialReply as (p: { text?: string; delta?: string }) => Promise<boolean>;
  const r = await handler({});
  assert.equal(r, false);
  assert.deepEqual(got, []);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.error(`Failed: ${failedTests.join(', ')}`);
  process.exit(1);
}
