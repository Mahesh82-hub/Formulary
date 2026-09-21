import { Suspense } from "react";

import { ChatApp } from "@/components/chat/chat-app";

export default function ChatPage() {
  return (
    <Suspense fallback={<div className="min-h-dvh bg-[var(--background)]" />}>
      <ChatApp />
    </Suspense>
  );
}
