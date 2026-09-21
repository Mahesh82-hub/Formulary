import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

function withoutMarkdownNode<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const cleanProps = { ...props };
  delete cleanProps.node;
  return cleanProps;
}

export function MarkdownMessage({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={cn("message-copy", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, rehypeSanitize]}
        components={{
          a: (props) => (
            <a {...withoutMarkdownNode(props)} target="_blank" rel="noreferrer noopener" />
          ),
          table: (props) => (
            <div className="message-table-wrap">
              <table {...withoutMarkdownNode(props)} />
            </div>
          ),
          input: (props) => <input {...withoutMarkdownNode(props)} readOnly />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
