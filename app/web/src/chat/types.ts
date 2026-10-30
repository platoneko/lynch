import type { WatchCard } from '../watch/types';

/** Mirrors lynch_reader/chat: summaries from ChatService.list, items from chat/items.py. */
export type ChatState = 'running' | 'compacting' | 'idle' | 'dormant';

export type ChatKind = 'research' | 'ticker' | 'discover';

/** Which program runs a chat; `kind` stays what it is about. discover is always grok. */
export type ChatAgent = 'claude' | 'opencode' | 'grok';

export interface ChatSummary {
  id: string;
  kind: ChatKind; // discover: a Grok search of the KOL list on X
  ticker: string | null; // research: null until its framework is written
  title: string;
  model: string;
  effort: string;
  created_at: number; // seconds since the epoch
  updated_at: number;
  context_window: number | null;
  cost_usd?: number | null; // discover: what its turns have cost so far, in USD
  lookback_days?: number | null; // discover: how many days back the KOL search looks, picked at creation
  archived: boolean;
  state: ChatState;
  agent: ChatAgent;
  agent_label: string; // e.g. Claude Code
  model_label: string; // the model's name, or its id when the server knows none
  opencode_session?: string | null;
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
  discover: DiscoverOptions;
  agents: { id: ChatAgent; label: string }[]; // the agents a 研究 / 讨论 chat can run on
  default_agent: ChatAgent;
  opencode: { models: { id: string; label: string }[]; default_model: string };
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
  | { kind: 'text'; key: string; text: string; candidates?: Candidate[]; candidates_dropped?: number; watch?: WatchCard }
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

/** A Grok model that can search X (server chat/grok.py `GrokModel.public`). */
export interface DiscoverModel {
  id: string;
  label: string;
  windows: number[]; // context windows in tokens
  efforts: string[];
  default_effort: string;
}

export interface DiscoverOptions {
  models: DiscoverModel[];
  default_model: string | null;
  default_window: number;
  lookback_days: number[]; // the lookback choices, in days
  default_lookback_days: number;
  hint: string | null; // why the list is empty
}

export type Stance = 'bullish' | 'bearish' | 'neutral';

/** One KOL post behind a candidate (server chat/candidates.py). */
export interface CandidateSource {
  handle: string; // without "@"
  stance: Stance;
  reason: string;
  quote: string; // the post's own words
  url: string;
  posted_at: string; // ISO 8601
}

export interface Candidate {
  ticker: string; // CODE.MARKET
  name: string; // always set for HK / SH / SZ
  sources: CandidateSource[];
}

/** A row of the candidate list (server `CandidateStore`). */
export interface SavedCandidate extends Candidate {
  sources: (CandidateSource & { saved_at: number; conv_id: string })[];
  first_saved_at: number;
  updated_at: number;
  archived_at: number | null; // seconds since the epoch; deleted 30 days later
}
