# MCP architecture

Dr. Insilico uses FastMCP as its tool protocol boundary. FastAPI remains the application API
that owns authentication, conversations, persistence, and browser-facing streaming.

## Current development topology

The API process contains a small internal FastMCP server and connects to it with FastMCP's
in-memory client transport. This avoids an unnecessary subprocess or network hop during local
development while exercising the same MCP discovery and invocation protocol used by remote
servers.

The chat orchestrator depends only on `FastMCPToolClient`. It will:

1. discover MCP tool definitions;
2. present the permitted tool definitions to the selected LLM provider;
3. validate and invoke requested tools through the FastMCP client;
4. persist each invocation in `tool_executions`;
5. return tool results to the LLM until a final assistant response is produced;
6. suppress exact duplicate name-and-argument calls within the same assistant run;
7. allow up to 8 research rounds and 12 tool attempts per user turn by default; and
8. when either budget is reached, disable tools for one final synthesis response that identifies
   remaining gaps and lets the user decide whether to continue research in the next message.

The limits reset for every user turn. Budget exhaustion is persisted as a completed run with
`completion_reason=research_budget_reached`; it is not treated as a provider failure.

Model-selected FDA label section names use a permissive string schema at the provider boundary,
then aliases and the supported allowlist are applied inside the MCP tool. Unsupported names are
reported in tool caveats instead of causing a provider-side schema rejection. If a provider still
rejects a generated tool call with `tool_use_failed`, the orchestrator performs one tools-disabled
synthesis from evidence already collected and records `completion_reason=tool_validation_recovered`.
For Groq continuations, the active system instruction is replaced on every request and tool fields
are omitted entirely during synthesis. If the provider nevertheless attempts a tool, the
orchestrator retries once with a clean conversation containing a bounded transcript of previously
collected evidence and records `clean_synthesis_fallback_used=true`.

## Interactive clarification

Both normal chat and the simulation-evidence workflow support interactive clarification. For an
ordinary request with one material missing choice, the model calls `request_user_clarification`
with one focused question and suggested answers. The simulator workflow continues to use
`prepare_bioequivalence_evidence_request`. Either result pauses orchestration immediately rather
than asking the model to rewrite questions as prose.

The assistant message stores a versioned `clarification` content block containing stable question
IDs, field IDs, suggestions, custom-answer support, the partially resolved context, and missing
fields. The chat UI renders this as an active card. Submitting a suggestion or "Something else"
response creates the next user message and resumes the same conversation branch. The assistant can
then ask the next question as a new round or finish the task; submitted cards become read-only in
the transcript. Each paused run is recorded with `completion_reason=awaiting_clarification` and
`awaiting_user_input=true`.

Suggested answers are reserved for concrete values that can be selected verbatim. Open-ended
questions—such as requesting a drug name, product identifier, date, or study detail—use an empty
suggestion list and render only the custom text input. The MCP boundary also removes instructional
or example-like suggestions and hides the suggestion area when fewer than two valid choices remain.

## Production evolution

FDA, document retrieval, and other pharma integrations should be separate focused MCP servers.
They can use Streamable HTTP and authentication in production. The application-owned MCP gateway
will namespace their tools and enforce per-user authorization, timeouts, approval policy, and
result-size limits before invocation.

MCP servers do not receive browser session cookies, database sessions, or unrestricted application
credentials. Any user context passed to a tool must be explicit and scoped to that invocation.

Semantic retrieval remains behind the existing bounded evidence MCP tools. The implementation
uses local BGE-small ONNX embeddings and pgvector hybrid search, so MCP callers remain independent
of the embedding backend and never receive unrestricted vector-store access.
