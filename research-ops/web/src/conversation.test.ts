import { expect, test } from "vitest";
import { upsertConversationMessage } from "./conversation";
import type { BranchDetail, ConversationMessage } from "./types";

const message: ConversationMessage = {
  id: "message-1",
  branch_id: "branch-1",
  role: "assistant",
  kind: "message",
  content: "First response",
  metadata_json: {},
  created_at: "2026-07-14T00:00:00Z",
};

const detail = { messages: [] } as unknown as BranchDetail;

test("appends a newly streamed conversation message", () => {
  const updated = upsertConversationMessage(detail, message);
  expect(updated?.messages).toEqual([message]);
});

test("replaces the pending copy when the same message is confirmed", () => {
  const pending = upsertConversationMessage(detail, message);
  const confirmed = { ...message, metadata_json: { send_status: "submitted" } };
  const updated = upsertConversationMessage(pending, confirmed);
  expect(updated?.messages).toEqual([confirmed]);
});
