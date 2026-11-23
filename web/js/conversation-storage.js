// 刷新恢复只保存不透明的 conversation_id；不保存消息、查询、模型 Key 或任何全文内容。

export const CONVERSATION_STORAGE_KEY = 'citation.conversation-id.v1';

function validId(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 256 ? value : null;
}

function storageKey(scope) {
  return scope && scope !== 'guest' ? `${CONVERSATION_STORAGE_KEY}:${scope}` : CONVERSATION_STORAGE_KEY;
}

export function readConversationId(storage, scope) {
  try {
    return validId(storage?.getItem(storageKey(scope)));
  } catch {
    return null;
  }
}

export function writeConversationId(storage, conversationId, scope) {
  const id = validId(conversationId);
  if (!id) return clearConversationId(storage, scope);
  try {
    storage?.setItem(storageKey(scope), id);
  } catch {
    // 私密浏览或配额不足时恢复能力降级；当前内存会话仍可继续。
  }
  return id;
}

export function clearConversationId(storage, scope) {
  try {
    storage?.removeItem(storageKey(scope));
  } catch {
    // 无可用会话存储时无需清理。
  }
  return null;
}
