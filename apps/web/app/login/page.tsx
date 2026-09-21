import { LoginForm } from "@/components/auth/login-form";
import { BrandMark } from "@/components/brand-mark";
import { ThemeToggle } from "@/components/theme-toggle";

export default function LoginPage() {
  return (
    <main className="relative grid min-h-dvh overflow-hidden lg:grid-cols-[1.05fr_0.95fr]">
      <div className="absolute right-5 top-5 z-20">
        <ThemeToggle />
      </div>
      <section className="brand-hero relative hidden overflow-hidden p-12 text-white lg:flex lg:flex-col lg:justify-between">
        <div className="absolute inset-0 opacity-40">
          <div className="absolute -left-20 -top-24 size-[28rem] rounded-full bg-[#c97cf0]/20 blur-3xl" />
          <div className="absolute -bottom-24 -right-20 size-[30rem] rounded-full bg-[#d7b2f4]/10 blur-3xl" />
          <svg className="absolute inset-0 size-full opacity-20" aria-hidden="true">
            <defs>
              <pattern id="grid" width="42" height="42" patternUnits="userSpaceOnUse">
                <path d="M42 0H0V42" fill="none" stroke="currentColor" strokeWidth="0.5" />
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#grid)" />
          </svg>
        </div>
        <div className="relative flex items-center gap-3">
          <BrandMark className="size-11 rounded-2xl" />
          <span className="text-lg font-semibold tracking-tight">Formulary</span>
        </div>
        <div className="relative max-w-xl pb-8">
          <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/10 px-3 py-1.5 text-xs text-white/80">
            <span className="size-1.5 rounded-full bg-[#d7a3f2] shadow-[0_0_12px_#d7a3f2]" />
            Pharmaceutical intelligence, carefully grounded
          </div>
          <h1 className="text-balance text-5xl font-medium leading-[1.08] tracking-[-0.04em]">
            Research with clarity. Decide with context.
          </h1>
          <p className="mt-6 max-w-lg text-base leading-7 text-white/65">
            A focused workspace for pharmaceutical questions, evidence-aware answers, and the
            specialized tools your team trusts.
          </p>
        </div>
        <p className="relative text-xs text-white/40">
          Built for research support—not diagnosis or medical advice.
        </p>
      </section>

      <section className="app-shell flex min-h-dvh items-center justify-center px-6 py-16">
        <div className="w-full max-w-[420px]">
          <div className="mb-10 flex items-center gap-3 lg:hidden">
            <BrandMark className="size-11 rounded-2xl" />
            <span className="font-semibold tracking-tight">Formulary</span>
          </div>
          <LoginForm />
        </div>
      </section>
    </main>
  );
}
