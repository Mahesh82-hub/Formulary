"use client";

import { LoaderCircle } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { ChatHeader } from "@/components/chat/chat-header";
import { Composer } from "@/components/chat/composer";
import { MessageList, type ToolActivity } from "@/components/chat/message-list";
import type { ProviderChoice } from "@/components/chat/model-picker";
import { Sidebar } from "@/components/chat/sidebar";
import {
  ApiError,
  apiFetch,
  streamChatTurn,
  streamRegeneration,
} from "@/lib/api";
import type { ChatEvent, Conversation, ConversationDetail, Message, User } from "@/lib/types";

export function ChatApp() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedId = searchParams.get("conversation");
  const [user, setUser] = useState<User | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [assistantDraft, setAssistantDraft] = useState("");
  const [activities, setActivities] = useState<ToolActivity[]>([]);
  const [provider, setProvider] = useState<ProviderChoice>("default");
  const [model, setModel] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const loadConversations = useCallback(async () => {
    const items = await apiFetch<Conversation[]>("/api/v1/conversations");
    setConversations(items);
    return items;
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    const conversation = await apiFetch<ConversationDetail>(`/api/v1/conversations/${id}`);
    setDetail(conversation);
  }, []);

  useEffect(() => {
    Promise.all([
      apiFetch<User>("/api/v1/auth/me"),
      apiFetch<Conversation[]>("/api/v1/conversations"),
    ])
      .then(([currentUser, items]) => {
        setUser(currentUser);
        setConversations(items);
      })
      .catch((error) => {
        if (error instanceof ApiError && error.status === 401) router.replace("/login");
        else toast.error("Unable to load your workspace");
      })
      .finally(() => setLoading(false));
  }, [loadConversations, router]);

  useEffect(() => {
    if (selectedId) {
      apiFetch<ConversationDetail>(`/api/v1/conversations/${selectedId}`)
        .then((conversation) =>
          setDetail((current) =>
            current?.id === conversation.id &&
            current.messages.length > conversation.messages.length
              ? current
              : conversation,
          ),
        )
        .catch(() => {
          toast.error("Unable to load that conversation");
          router.replace("/chat");
        });
    }
  }, [router, selectedId]);

  function selectConversation(id: string) {
    router.replace(`/chat?conversation=${id}`);
  }

  function newConversation() {
    if (sending) return;
    setDetail(null);
    setAssistantDraft("");
    setActivities([]);
    router.replace("/chat");
  }

  async function ensureConversation() {
    if (detail) return detail;
    const conversation = await apiFetch<Conversation>("/api/v1/conversations", {
      method: "POST",
      body: JSON.stringify({}),
    });
    const created: ConversationDetail = { ...conversation, messages: [] };
    setDetail(created);
    setConversations((current) => [conversation, ...current]);
    router.replace(`/chat?conversation=${conversation.id}`);
    return created;
  }

  function handleEvent(event: ChatEvent) {
    if (event.type === "run.started") {
      setActivities((current) =>
        current.map((activity) =>
          activity.id === "pdf-attachment" ? { ...activity, status: "completed" } : activity,
        ),
      );
      const triggerId = String(event.data.trigger_message_id ?? "");
      setDetail((current) =>
        current
          ? {
              ...current,
              messages: current.messages.map((message) =>
                message.id.startsWith("pending-") ? { ...message, id: triggerId } : message,
              ),
            }
          : current,
      );
    }
    if (event.type === "tool.started") {
      setActivities((current) => [
        ...current,
        {
          id: String(event.data.tool_execution_id),
          name: String(event.data.tool_name),
          status: "running",
        },
      ]);
    }
    if (event.type === "tool.completed" || event.type === "tool.failed") {
      setActivities((current) =>
        current.map((activity) =>
          activity.id === String(event.data.tool_execution_id)
            ? { ...activity, status: event.type === "tool.completed" ? "completed" : "failed" }
            : activity,
        ),
      );
    }
    if (event.type === "message.delta") {
      setAssistantDraft((current) => current + String(event.data.delta ?? ""));
    }
    if (event.type === "run.failed") {
      const detailedMessage =
        typeof event.data.message === "string" ? event.data.message : null;
      const requestId =
        typeof event.data.request_id === "string" ? event.data.request_id : null;
      const statusCode =
        typeof event.data.status_code === "number" ? event.data.status_code : null;
      const diagnosticSuffix = [
        statusCode ? `HTTP ${statusCode}` : null,
        requestId ? `Request ${requestId}` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      toast.error(
        detailedMessage
          ? `Assistant error: ${String(event.data.code ?? "unknown")}`
          : "The assistant could not complete that response",
        {
          description: detailedMessage
            ? `${detailedMessage}${diagnosticSuffix ? ` (${diagnosticSuffix})` : ""}`
            : "Your message was saved. You can retry in a moment.",
        },
      );
    }
  }

  async function runAndRefresh(
    conversationId: string,
    operation: () => Promise<void>,
    options: { restoreDetailOnAbort?: ConversationDetail } = {},
  ) {
    setSending(true);
    setAssistantDraft("");
    setActivities([]);
    const controller = new AbortController();
    abortRef.current = controller;
    let aborted = false;
    try {
      await operation();
    } catch (error) {
      if ((error as Error).name === "AbortError") {
        aborted = true;
        if (options.restoreDetailOnAbort) setDetail(options.restoreDetailOnAbort);
      } else {
        toast.error(error instanceof ApiError ? error.message : "The assistant request failed");
      }
    } finally {
      abortRef.current = null;
      setSending(false);
      setAssistantDraft("");
      setActivities([]);
      if (aborted && options.restoreDetailOnAbort) {
        await loadConversations().catch(() => undefined);
      } else {
        await Promise.all([loadDetail(conversationId), loadConversations()]).catch(() => undefined);
      }
    }
  }

  async function sendMessage(text: string, pdf?: File) {
    const conversation = await ensureConversation();
    if (!conversation) return;
    const optimistic: Message = {
      id: `pending-${Date.now()}`,
      conversation_id: conversation.id,
      parent_message_id: conversation.active_leaf_message_id,
      supersedes_message_id: null,
      role: "user",
      status: "completed",
      content: [
        { type: "text", text },
        ...(pdf
          ? [
              {
                type: "document",
                media_type: "application/pdf",
                filename: pdf.name,
                byte_size: pdf.size,
              },
            ]
          : []),
      ],
      plain_text: text,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    setDetail((current) => {
      const base = current?.id === conversation.id ? current : conversation;
      return { ...base, messages: [...base.messages, optimistic] };
    });
    await runAndRefresh(conversation.id, () => {
      if (pdf) {
        setActivities([
          {
            id: "pdf-attachment",
            name: "PDF attachment",
            status: "running",
          },
        ]);
      }
      return streamChatTurn({
        conversationId: conversation.id,
        text,
        pdf,
        provider: provider === "default" ? undefined : provider,
        model: model.trim() || undefined,
        signal: abortRef.current?.signal,
        onEvent: handleEvent,
      });
    });
  }

  async function regenerate(message: Message) {
    if (!detail || sending) return;
    const previousDetail = detail;
    await runAndRefresh(
      detail.id,
      () =>
        streamRegeneration({
          conversationId: detail.id,
          messageId: message.id,
          provider: provider === "default" ? undefined : provider,
          model: model.trim() || undefined,
          signal: abortRef.current?.signal,
          onEvent: handleEvent,
        }),
      {
        restoreDetailOnAbort: message.role === "assistant" ? previousDetail : undefined,
      },
    );
  }

  async function editMessage(message: Message, text: string) {
    if (!detail || sending) return;
    try {
      const replacement = await apiFetch<Message>(
        `/api/v1/conversations/${detail.id}/messages/${message.id}/edit`,
        { method: "POST", body: JSON.stringify({ text }) },
      );
      await loadDetail(detail.id);
      await regenerate(replacement);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Unable to edit the message");
    }
  }

  async function archiveConversation(conversation: Conversation) {
    const nextStatus = conversation.status === "archived" ? "active" : "archived";
    await apiFetch(`/api/v1/conversations/${conversation.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: nextStatus }),
    });
    await loadConversations();
    if (detail?.id === conversation.id) await loadDetail(conversation.id);
  }

  async function deleteConversation(conversation: Conversation) {
    if (!window.confirm(`Delete “${conversation.title ?? "New conversation"}”?`)) return;
    await apiFetch(`/api/v1/conversations/${conversation.id}`, { method: "DELETE" });
    if (selectedId === conversation.id) newConversation();
    await loadConversations();
    toast.success("Conversation deleted");
  }

  async function logout() {
    await apiFetch("/api/v1/auth/logout", { method: "POST" });
    router.replace("/login");
  }

  if (loading || !user) {
    return (
      <div className="grid min-h-dvh place-items-center bg-[var(--background)] text-[var(--muted)]">
        <LoaderCircle className="size-5 animate-spin" />
      </div>
    );
  }

  return (
    <main className="app-shell flex h-dvh overflow-hidden">
      <Sidebar
        conversations={conversations}
        selectedId={selectedId}
        user={user}
        open={sidebarOpen}
        onOpenChange={setSidebarOpen}
        onNew={newConversation}
        onSelect={selectConversation}
        onArchive={(conversation) => archiveConversation(conversation).catch(() => toast.error("Unable to update conversation"))}
        onDelete={(conversation) => deleteConversation(conversation).catch(() => toast.error("Unable to delete conversation"))}
        onLogout={() => logout().catch(() => toast.error("Unable to sign out"))}
      />
      <section className="flex min-w-0 flex-1 flex-col">
        <ChatHeader conversation={detail} onOpenSidebar={() => setSidebarOpen(true)} />
        <MessageList
          messages={detail?.messages ?? []}
          sending={sending}
          assistantDraft={assistantDraft}
          activities={activities}
          onStarter={sendMessage}
          onEdit={editMessage}
          onRegenerate={regenerate}
          onClarificationSubmit={sendMessage}
        />
        <Composer
          disabled={detail?.status === "archived"}
          sending={sending}
          provider={provider}
          model={model}
          onProviderChange={setProvider}
          onModelChange={setModel}
          onSend={sendMessage}
          onStop={() => abortRef.current?.abort()}
        />
      </section>
    </main>
  );
}
