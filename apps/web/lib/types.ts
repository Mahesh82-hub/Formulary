export type User = {
  id: string;
  email: string;
  display_name: string | null;
};

export type Conversation = {
  id: string;
  title: string | null;
  status: "active" | "archived";
  model_preferences: Record<string, unknown>;
  active_leaf_message_id: string | null;
  created_at: string;
  updated_at: string;
};

export type Message = {
  id: string;
  conversation_id: string;
  parent_message_id: string | null;
  supersedes_message_id: string | null;
  role: "user" | "assistant" | "system" | "tool";
  status: "pending" | "streaming" | "completed" | "failed" | "cancelled";
  content: Array<Record<string, unknown>>;
  plain_text: string;
  created_at: string;
  updated_at: string;
};

export type PdfAttachment = {
  type: "document";
  media_type: "application/pdf";
  filename: string;
  byte_size: number;
  pages?: number;
  native_text_pages?: number;
  ocr_pages?: number;
  tables?: number;
  graph_candidate_pages?: number[];
  warnings?: string[];
  extracted_character_count?: number;
  included_character_count?: number;
  truncated?: boolean;
};

export type ClarificationQuestion = {
  question_id: string;
  field_ids: string[];
  prompt: string;
  reason: string;
  suggestions: string[];
  allow_custom: boolean;
};

export type ClarificationBlock = {
  type: "clarification";
  version: 1;
  status: "awaiting_user";
  task_type: "simulation_evidence_research" | "general_chat";
  title: string;
  context: Record<string, unknown>;
  missing_required_fields: string[];
  questions: ClarificationQuestion[];
  allow_additional_question: boolean;
  submit_label: string;
};

export type ConversationDetail = Conversation & { messages: Message[] };

export type ChatEventType =
  | "run.started"
  | "tool.started"
  | "tool.completed"
  | "tool.failed"
  | "message.delta"
  | "message.completed"
  | "run.failed";

export type ChatEvent = {
  type: ChatEventType;
  data: Record<string, unknown>;
};

export type Significance = "high" | "medium" | "low";

export type RegulatoryEventType =
  | "new_drug_approval"
  | "generic_approval"
  | "new_indication"
  | "manufacturing_change"
  | "safety_program_change"
  | "bioequivalence_supplement"
  | "labeling_supplement"
  | "other_supplement"
  | "label_change"
  | "trial_status_change"
  | "trial_registered"
  | "trial_results_posted";

export type LabelSectionChange = {
  section: string;
  label: string;
  change: "added" | "removed" | "revised";
  before: string | null;
  after: string | null;
  added_terms: string[];
  removed_terms: string[];
};

export type RegulatoryEvent = {
  id: string;
  source: string;
  event_type: RegulatoryEventType;
  significance: Significance;
  headline: string;
  summary: string;
  subject: string;
  drug_names: string[];
  sponsor: string | null;
  occurred_on: string;
  detected_at: string;
  source_url: string;
  details: {
    changes?: LabelSectionChange[];
    labels?: Array<{ set_id: string; manufacturer: string | null; url: string; version: string }>;
    documents?: Array<{ type: string | null; url: string; date: string | null }>;
    why_stopped?: string | null;
    nct_id?: string;
    [key: string]: unknown;
  };
  provenance: { source?: string; api_url?: string; retrieved_at?: string; disclaimer?: string };
};

export type EventFeed = {
  events: RegulatoryEvent[];
  next_cursor: string | null;
  last_7_days: Record<Significance, number>;
};

export type Watch = {
  id: string;
  name: string;
  terms: string[];
  event_types: RegulatoryEventType[];
  min_significance: Significance;
  notify_email: boolean;
  slack_configured: boolean;
  slack_webhook_hint: string | null;
  active: boolean;
  last_notified_at: string | null;
  last_delivery_error: string | null;
  created_at: string;
};

export type MonitorRun = {
  id: string;
  detector: string;
  status: "running" | "completed" | "failed" | "skipped";
  window_start: string;
  window_end: string;
  records_scanned: number;
  events_created: number;
  baselines_recorded: number;
  error: string | null;
  started_at: string;
  completed_at: string | null;
};
