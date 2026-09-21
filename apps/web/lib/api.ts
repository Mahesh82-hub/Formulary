import type { ChatEvent, ChatEventType } from "@/lib/types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    let message = "Something went wrong";
    try {
      const body = (await response.json()) as {
        detail?: string | Array<{ msg?: string; loc?: Array<string | number> }>;
      };
      if (typeof body.detail === "string") message = body.detail;
      if (Array.isArray(body.detail)) {
        message = body.detail
          .map((error) => {
            const location = error.loc?.slice(1).join(".");
            return `${location ? `${location}: ` : ""}${error.msg ?? "Invalid value"}`;
          })
          .join("; ");
      }
    } catch {
      // Keep the safe fallback message for non-JSON failures.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

type StreamTurnOptions = {
  conversationId: string;
  text: string;
  provider?: "openai" | "groq";
  model?: string;
  parentMessageId?: string;
  pdf?: File;
  signal?: AbortSignal;
  onEvent: (event: ChatEvent) => void;
};

export async function streamChatTurn(options: StreamTurnOptions) {
  const path = options.pdf ? "turns/pdf" : "turns";
  let body: BodyInit;
  const headers = new Headers({ Accept: "text/event-stream" });
  if (options.pdf) {
    const form = new FormData();
    form.set("text", options.text);
    form.set("pdf", options.pdf);
    if (options.provider) form.set("provider", options.provider);
    if (options.model) form.set("model", options.model);
    if (options.parentMessageId) form.set("parent_message_id", options.parentMessageId);
    body = form;
  } else {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify({
      text: options.text,
      provider: options.provider,
      model: options.model || undefined,
      parent_message_id: options.parentMessageId,
    });
  }

  const response = await fetch(
    `${API_BASE_URL}/api/v1/conversations/${options.conversationId}/${path}`,
    {
      method: "POST",
      credentials: "include",
      headers,
      signal: options.signal,
      body,
    },
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new ApiError(body.detail ?? "Unable to start the assistant", response.status);
  }
  await consumeEventStream(response, options.onEvent);
}

type StreamRegenerationOptions = {
  conversationId: string;
  messageId: string;
  provider?: "openai" | "groq";
  model?: string;
  signal?: AbortSignal;
  onEvent: (event: ChatEvent) => void;
};

export async function streamRegeneration(options: StreamRegenerationOptions) {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/conversations/${options.conversationId}/messages/${options.messageId}/regenerate`,
    {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      signal: options.signal,
      body: JSON.stringify({
        provider: options.provider,
        model: options.model || undefined,
      }),
    },
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new ApiError(body.detail ?? "Unable to regenerate the response", response.status);
  }
  await consumeEventStream(response, options.onEvent);
}

async function consumeEventStream(
  response: Response,
  onEvent: (event: ChatEvent) => void,
) {
  if (!response.body) throw new ApiError("Streaming is unavailable", 500);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const eventLine = frame.split("\n").find((line) => line.startsWith("event: "));
      const dataLine = frame.split("\n").find((line) => line.startsWith("data: "));
      if (!eventLine || !dataLine) continue;
      const type = eventLine.slice(7) as ChatEventType;
      const data = JSON.parse(dataLine.slice(6)) as Record<string, unknown>;
      onEvent({ type, data });
    }
    if (done) break;
  }
}
