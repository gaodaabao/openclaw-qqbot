/**
 * 静默回复识别。
 *
 * 核心 openclaw 有等价的判定（openclaw 的 tokens 模块：isSilentReplyText /
 * isSilentReplyPrefixText），但没有从 openclaw / openclaw/plugin-sdk 导出，
 * 通道侧无法直接引用，只能在这里按同样语义实现一份。
 *
 * 语义（对齐核心当前实现）：
 * - 精确匹配：^\s*<token>(?:\s+<token>)*\s*（大小写不敏感）
 * - 前缀匹配：全大写、长度 >= 2、只含 A-Z_，且是某个 token 的前缀
 *
 * token 取自核心的 SILENT_REPLY_TOKEN / HEARTBEAT_TOKEN。
 */
const QQ_SILENT_REPLY_TOKENS = ['NO_REPLY', 'HEARTBEAT_OK'] as const;

/** 精确匹配：^\s*<token>(?:\s+<token>)*\s*（大小写不敏感） */
const QQ_SILENT_REPLY_EXACT_RE = new RegExp(
  `^\\s*(?:${QQ_SILENT_REPLY_TOKENS.join('|')})(?:\\s+(?:${QQ_SILENT_REPLY_TOKENS.join('|')}))*\\s*$`,
  'i',
);

/**
 * 判断一段文本是不是「静默回复」——即不应该真的发到聊天里的内容。
 */
export function isQQBotSilentReply(text: string | undefined | null): boolean {
  if (!text || typeof text !== 'string') return false;
  const trimmed = text.trim();
  if (!trimmed) return false;
  if (QQ_SILENT_REPLY_EXACT_RE.test(trimmed)) return true;
  if (trimmed !== trimmed.toUpperCase()) return false;
  if (trimmed.length < 2) return false;
  if (/[^A-Z_]/.test(trimmed)) return false;
  return QQ_SILENT_REPLY_TOKENS.some((token) => token.startsWith(trimmed));
}
