# Formulary database design proposal

Status: architecture decisions approved. Authentication/chat, ingestion, and local BGE hybrid
retrieval migrations are implemented; remaining domains are applied incrementally.

## Design principles

- Use PostgreSQL as the system of record and pgvector for semantic retrieval.
- Use direct `user_id` ownership for the single-tenant first release.
- Use UUIDv7 identifiers, `timestamptz` timestamps, and UTC everywhere.
- Keep core relationships relational; use JSONB only for variable provider, tool, and source payloads.
- Store uploaded binaries and extraction artifacts on the local filesystem initially, behind a storage adapter.
- Keep secrets out of JSONB and normal columns; store only references to a secrets manager.
- Preserve raw messages and provenance. Summaries and memories are derived, replaceable data.
- Use text columns with check constraints for evolving statuses instead of PostgreSQL enums.

## Proposed tables

### Identity and authentication

| Table | Purpose | Important columns |
| --- | --- | --- |
| `users` | Product user profile | `id`, `email`, `display_name`, `status`, timestamps |
| `otp_challenges` | Short-lived, single-use email login challenge | `email`, `code_digest`, attempts, expiry and consumption timestamps |
| `user_sessions` | Revocable browser session | `user_id`, `token_digest`, expiry, revocation and activity timestamps |
| `auth_events` | Append-only authentication security trail | optional `user_id`, `email`, `event_type`, `success`, request metadata |

### Chat and orchestration

| Table | Purpose | Important columns |
| --- | --- | --- |
| `conversations` | Chat metadata | `user_id`, `title`, `status`, `active_leaf_message_id`, `model_preferences`, `deleted_at` |
| `messages` | Durable user, assistant, system, and tool messages | `conversation_id`, `parent_message_id`, `role`, `status`, `content`, `plain_text`, `created_at` |
| `assistant_runs` | One orchestration attempt, including retries and usage | `conversation_id`, `trigger_message_id`, `response_message_id`, `provider`, `model`, `status`, `state`, `usage`, timestamps |
| `tool_executions` | MCP, skill, API, and internal tool execution history | `run_id`, `tool_kind`, `tool_name`, `arguments`, `result`, `error`, `status`, approval and timing fields |
| `citations` | Evidence attached to an assistant message | `message_id`, optional `chunk_id`, optional `external_record_id`, `label`, `locator`, `score` |

`messages.content` is JSONB so a message can contain ordered text, image, file, reasoning-summary, and tool-result parts. `plain_text` remains separately searchable. `parent_message_id` allows editing, regeneration, and branching without copying an entire conversation.

### Documents and retrieval

| Table | Purpose | Important columns |
| --- | --- | --- |
| `data_sources` | Upload, FDA API, website, or other source configuration | `user_id`, `kind`, `name`, `config`, `secret_ref`, `status` |
| `documents` | Stable logical document identity | `user_id`, `data_source_id`, `external_key`, `title`, `metadata`, `deleted_at` |
| `document_versions` | Immutable version of source content | `document_id`, `version`, `checksum`, `object_uri`, extraction status and timestamps |
| `document_chunks` | Citation and full-text retrieval unit | content, provenance, `search_vector`, embedding status and metrics |
| `document_chunk_embeddings` | Model-safe semantic segment linked to its citation chunk | segment content/hash, BGE model/revision/backend, dimensions, `vector(384)` |
| `ingestion_jobs` | Observable and retryable ingestion work | source/document references, `status`, `progress`, attempt, lease, error and timing fields |

Retrieval combines PostgreSQL full-text ranking with cosine search over local
`BAAI/bge-small-en-v1.5` embeddings using reciprocal-rank fusion. Model-safe segments prevent
long citation chunks from being silently truncated and resolve back to the authoritative parent
chunk. The configured Hugging Face commit is pinned and stored with each embedding for
reproducibility.

### Context and memory

| Table | Purpose | Important columns |
| --- | --- | --- |
| `conversation_summaries` | Versioned rolling summary at a message boundary | `conversation_id`, `through_message_id`, `summary`, `structured_state`, `model`, `created_at` |
| `memories` | Atomic, cross-chat facts or preferences with provenance | `user_id`, source message/conversation, `scope`, `kind`, `content`, `structured_value`, `confidence`, validity, `embedding`, `status` |

Raw messages remain authoritative. Summaries reduce prompt size; memories are selectively retrieved and can be superseded, expired, or deleted independently.

### Skills, MCP, and external pharmaceutical data

| Table | Purpose | Important columns |
| --- | --- | --- |
| `skills` | Stable skill identity and ownership | optional `user_id`, `slug`, `name`, `status` |
| `skill_versions` | Immutable skill definition | `skill_id`, `version`, `instructions`, `manifest`, `checksum`, `created_by` |
| `user_skills` | Enables and configures a skill for a user | `user_id`, `skill_id`, `enabled`, `config`, `secret_ref` |
| `mcp_servers` | Registered MCP server definition | owner/scope, `name`, `transport`, endpoint, non-secret config, `secret_ref`, `status` |
| `external_records` | Cache and provenance for FDA or other external API records | `data_source_id`, `record_type`, `external_id`, `payload`, `normalized`, `checksum`, fetch and expiry timestamps |
| `audit_events` | Append-only security and compliance trail | actor, action, resource, request ID, metadata, timestamp |

FDA payloads should initially be cached as JSONB with a small normalized projection. Frequently queried pharmaceutical fields can later be promoted into dedicated relational tables or materialized views without coupling chat history to one FDA response shape.

## Index strategy

- B-tree indexes on every foreign key and user/list access pattern.
- `(user_id, updated_at DESC)` for user-facing lists.
- `(conversation_id, created_at)` and `parent_message_id` for message traversal.
- GIN full-text index on `document_chunks.search_vector`.
- HNSW cosine index on `document_chunk_embeddings.embedding`.
- Equivalent full-text/vector indexes on active memories.
- Unique `(data_source_id, record_type, external_id)` on cached external records.
- Targeted JSONB indexes only after real query patterns appear.

## Migration sequence

1. Users, email OTP, sessions, conversations, messages, runs, and tool executions.
2. Data sources, documents, versions, ingestion jobs, chunks, and citations.
3. Conversation summaries and cross-chat memories.
4. Skills, MCP servers, external records, and audit events.

## Confirmed initial decisions

1. Single tenancy with direct user ownership; no workspace tables initially.
2. Application-owned, passwordless email OTP with hashed challenges and revocable sessions.
3. Message editing and regeneration create branches; conversations track the selected active leaf.
4. Local BGE-small ONNX embeddings use 384 dimensions and remain provider-independent from the
   hosted generation LLM.
5. Local filesystem storage during development, isolated behind a replaceable storage interface.
6. Hosted LLMs such as OpenAI or Groq are interchangeable generation providers; embeddings remain local and provider-independent.
