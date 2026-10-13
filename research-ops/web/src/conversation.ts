import type { BranchDetail, ConversationMessage } from "./types";

export function upsertConversationMessage(
  detail: BranchDetail | undefined,
  message: ConversationMessage,
): BranchDetail | undefined {
  if (!detail) return detail;
  const index = detail.messages.findIndex((item) => item.id === message.id);
  if (index === -1) return { ...detail, messages: [...detail.messages, message] };
  const messages = [...detail.messages];
  messages[index] = message;
  return { ...detail, messages };
}
