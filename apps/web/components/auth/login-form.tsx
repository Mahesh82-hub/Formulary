"use client";

import { ArrowLeft, ArrowRight, Check, LoaderCircle, Mail } from "lucide-react";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

type Step = "email" | "code";

export function LoginForm() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [challengeId, setChallengeId] = useState<string>();
  const [submitting, setSubmitting] = useState(false);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    apiFetch<User>("/api/v1/auth/me")
      .then(() => router.replace("/chat"))
      .catch(() => setChecking(false));
  }, [router]);

  async function requestCode(event?: FormEvent) {
    event?.preventDefault();
    setSubmitting(true);
    try {
      const response = await apiFetch<{ challenge_id: string }>("/api/v1/auth/otp/request", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      setChallengeId(response.challenge_id);
      setCode("");
      setStep("code");
      toast.success("Login code sent", { description: `Check ${email}` });
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Unable to send a login code");
    } finally {
      setSubmitting(false);
    }
  }

  async function verifyCode(event: FormEvent) {
    event.preventDefault();
    if (!challengeId) return;
    setSubmitting(true);
    try {
      await apiFetch<{ user: User }>("/api/v1/auth/otp/verify", {
        method: "POST",
        body: JSON.stringify({ challenge_id: challengeId, code }),
      });
      toast.success("Welcome to Formulary");
      router.replace("/chat");
      router.refresh();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "That code did not work");
    } finally {
      setSubmitting(false);
    }
  }

  if (checking) {
    return (
      <div className="grid min-h-64 place-items-center text-[var(--muted)]">
        <LoaderCircle className="size-5 animate-spin" />
      </div>
    );
  }

  return (
    <div className="animate-fade-up">
      <div className="mb-8">
        <h2 className="text-3xl font-semibold tracking-[-0.035em]">
          {step === "email" ? "Sign in to continue" : "Enter your login code"}
        </h2>
        <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
          {step === "email"
            ? "No password to remember. We’ll email you a short-lived verification code."
            : `We sent a six-digit code to ${email}. It expires shortly.`}
        </p>
      </div>

      {step === "email" ? (
        <form onSubmit={requestCode} className="space-y-5">
          <label className="block space-y-2">
            <span className="text-sm font-medium">Work email</span>
            <div className="relative">
              <Mail className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-[var(--muted)]" />
              <Input
                className="h-12 pl-10"
                type="email"
                autoComplete="email"
                autoFocus
                required
                placeholder="you@company.com"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
          </label>
          <Button className="h-12 w-full" type="submit" disabled={submitting || !email}>
            {submitting ? <LoaderCircle className="animate-spin" /> : <ArrowRight />}
            Continue with email
          </Button>
        </form>
      ) : (
        <form onSubmit={verifyCode} className="space-y-5">
          <label className="block space-y-2">
            <span className="text-sm font-medium">Verification code</span>
            <Input
              className="h-14 text-center font-mono text-2xl tracking-[0.35em]"
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
              required
              maxLength={6}
              pattern="[0-9]{6}"
              placeholder="000000"
              value={code}
              onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
            />
          </label>
          <Button className="h-12 w-full" type="submit" disabled={submitting || code.length !== 6}>
            {submitting ? <LoaderCircle className="animate-spin" /> : <Check />}
            Verify and sign in
          </Button>
          <div className="flex items-center justify-between text-xs">
            <button
              type="button"
              className="focus-ring flex cursor-pointer items-center gap-1.5 rounded-lg p-1 text-[var(--muted)] hover:text-[var(--foreground)]"
              onClick={() => setStep("email")}
            >
              <ArrowLeft className="size-3.5" /> Change email
            </button>
            <button
              type="button"
              className="focus-ring cursor-pointer rounded-lg p-1 font-medium text-[var(--primary)] hover:underline"
              disabled={submitting}
              onClick={() => requestCode()}
            >
              Send a new code
            </button>
          </div>
        </form>
      )}

      <p className="mt-9 border-t border-[var(--border)] pt-6 text-xs leading-5 text-[var(--muted)]">
        By continuing, you acknowledge that responses are for research support and must be
        independently verified before clinical or regulatory use.
      </p>
    </div>
  );
}
