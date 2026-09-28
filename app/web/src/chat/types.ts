/** Mirrors lynch_reader/chat: summaries from ChatService.list, items from chat/items.py. */
export type ChatState = 'running' | 'compacting' | 'idle' | 'dormant';

export interface ChatSummary {
  id: string;
  kind: 'research' | 'ticker';
  ticker: string | null; // research: null until its framework is written
  title: string;
  model: string;
  effort: string;
  created_at: number; // seconds since the epoch
  updated_at: number;
  context_window: number | null;
  archived: boolean;
  state: ChatState;
}

export interface Usage {
  five_hour: number | null; // 0..1 of the subscription window
  seven_day: number | null;
  status: string | null;
  resets_at: number | null;
}

export interface ChatList {
  chats: ChatSummary[];
  running_count: number;
  usage: Usage | null;
}

export interface ChatOptions {
  models: { id: string; label: string }[];
  efforts: string[];
  default_model: string;
  default_effort: string;
}

export interface ContextUsage {
  tokens: number | null; // null right after a compaction
  window: number | null;
}

export interface ToolItem {
  kind: 'tool';
  key: string;
  id: string;
  name: string;
  input: Record<string, unknown>;
  output: string | null; // null while the call runs
  is_error: boolean;
  denied: boolean; // refused by the whitelist
}

export type ChatItem =
  | { kind: 'user'; key: string; uuid: string; text: string }
  | { kind: 'text'; key: string; text: string }
  | ToolItem
  | { kind: 'compact'; key: string; trigger: string | null; pre_tokens: number | null; post_tokens: number | null }
  | { kind: 'notice'; key: string; text: string };

export interface ToolResult {
  kind: 'tool_result';
  id: string;
  output: string;
  is_error: boolean;
  denied: boolean;
}

/** A skill edit claude waits on the phone to allow (chat/approvals.py `Approval.public`). */
export interface Approval {
  request_id: string;
  tool_use_id: string;
  tool: string;
  path: string; // relative to the lynch repo
  diff: string; // unified diff, or a one-line note when there is none
}

export interface Snapshot {
  conv: ChatSummary;
  items: ChatItem[];
  partial: string;
  retry: string | null; // the API request claude is retrying, until the model answers or the turn ends
  usage: Usage | null;
  context: ContextUsage;
  approvals: Approval[];
}

export type ChatEvent =
  | { type: 'delta'; text: string }
  | { type: 'item'; item: ChatItem }
  | { type: 'tool_result'; result: ToolResult }
  | { type: 'state'; state: ChatState }
  | { type: 'retry'; text: string | null }
  | { type: 'usage'; usage: Usage }
  | { type: 'context'; context: ContextUsage }
  | { type: 'conv'; conv: ChatSummary }
  | { type: 'approval'; approval: Approval }
  | { type: 'approval_done'; request_id: string };
