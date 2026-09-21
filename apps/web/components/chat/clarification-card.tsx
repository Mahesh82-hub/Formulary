"use client";

import { Check, CircleHelp, CornerDownRight } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import type { ClarificationBlock } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ClarificationCard({
  block,
  disabled,
  onSubmit,
}: {
  block: ClarificationBlock;
  disabled: boolean;
  onSubmit: (response: string) => void;
}) {
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});
  const [additionalText, setAdditionalText] = useState("");

  const hasResponse = useMemo(
    () =>
      Object.values(selections).some(Boolean) ||
      Object.values(customAnswers).some((value) => value.trim()) ||
      additionalText.trim().length > 0,
    [additionalText, customAnswers, selections],
  );

  function submit() {
    if (disabled || !hasResponse) return;
    const answers = block.questions.flatMap((question) => {
      const selected = selections[question.question_id]?.trim();
      const custom = customAnswers[question.question_id]?.trim();
      const response = [selected, custom].filter(Boolean).join("; ");
      return response ? [`- ${question.question_id}: ${response}`] : [];
    });
    const additional = additionalText.trim();
    onSubmit(
      [
        "Clarification response for the ongoing research task:",
        ...answers,
        ...(additional ? [`- additional_details_or_question: ${additional}`] : []),
        "Please continue the original task using these details.",
      ].join("\n"),
    );
  }

  return (
    <section className="mt-4 overflow-hidden rounded-2xl border border-[var(--primary)]/20 bg-[var(--surface)] shadow-sm">
      <div className="border-b border-[var(--border)] bg-[var(--primary-soft)]/55 px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <CircleHelp className="size-4 text-[var(--primary)]" />
          {block.title}
        </div>
        <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
          Your answers stay attached to this conversation and the research continues from here.
        </p>
      </div>

      <div className="space-y-5 p-4">
        {block.questions.map((question, index) => (
          <fieldset key={question.question_id} disabled={disabled} className="space-y-2.5">
            <legend className="text-sm font-medium leading-5">
              <span className="mr-2 text-[var(--muted)]">{index + 1}.</span>
              {question.prompt}
            </legend>
            {question.suggestions.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {question.suggestions.map((suggestion) => {
                  const selected = selections[question.question_id] === suggestion;
                  return (
                    <button
                      key={suggestion}
                      type="button"
                      onClick={() =>
                        setSelections((current) => ({
                          ...current,
                          [question.question_id]: selected ? "" : suggestion,
                        }))
                      }
                      className={cn(
                        "focus-ring inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1.5 text-left text-xs leading-4 transition",
                        selected
                          ? "border-[var(--primary)] bg-[var(--primary-soft)] text-[var(--foreground)]"
                          : "border-[var(--border)] bg-[var(--surface-soft)] text-[var(--muted)] hover:border-[var(--primary)]/45 hover:text-[var(--foreground)]",
                        disabled && "cursor-default opacity-70",
                      )}
                    >
                      {selected && <Check className="size-3" />}
                      {suggestion}
                    </button>
                  );
                })}
              </div>
            )}
            {question.allow_custom && (
              <input
                value={customAnswers[question.question_id] ?? ""}
                onChange={(event) =>
                  setCustomAnswers((current) => ({
                    ...current,
                    [question.question_id]: event.target.value,
                  }))
                }
                placeholder={
                  question.suggestions.length
                    ? block.task_type === "general_chat"
                      ? "Something else? Type here…"
                      : "Add specifics, such as strength, product, or study context…"
                    : "Enter your answer…"
                }
                className="focus-ring h-10 w-full rounded-xl border border-[var(--border)] bg-[var(--surface-soft)] px-3 text-sm outline-none placeholder:text-[var(--muted)]/65"
              />
            )}
            <p className="text-[11px] leading-4 text-[var(--muted)]">{question.reason}</p>
          </fieldset>
        ))}

        {block.allow_additional_question && (
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor={`clarification-${block.task_type}`}>
              Additional detail or follow-up question
            </label>
            <textarea
              id={`clarification-${block.task_type}`}
              rows={2}
              value={additionalText}
              disabled={disabled}
              onChange={(event) => setAdditionalText(event.target.value)}
              placeholder="Add anything else the research should consider…"
              className="focus-ring w-full resize-none rounded-xl border border-[var(--border)] bg-[var(--surface-soft)] px-3 py-2 text-sm leading-5 outline-none placeholder:text-[var(--muted)]/65"
            />
          </div>
        )}

        <div className="flex items-center justify-between gap-3 border-t border-[var(--border)] pt-3">
          <span className="text-[11px] text-[var(--muted)]">
            {disabled ? "Response submitted" : "You can also answer in the main message box."}
          </span>
          <Button size="sm" disabled={disabled || !hasResponse} onClick={submit}>
            {block.submit_label}
            <CornerDownRight className="size-3.5" />
          </Button>
        </div>
      </div>
    </section>
  );
}
