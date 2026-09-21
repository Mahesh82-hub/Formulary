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
