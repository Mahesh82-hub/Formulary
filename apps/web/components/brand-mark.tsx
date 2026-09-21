import { cn } from "@/lib/utils";

export function BrandMark({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "brand-mark relative grid size-9 shrink-0 place-items-center overflow-hidden rounded-xl",
        className,
      )}
      aria-hidden="true"
    >
      <svg viewBox="0 0 32 32" className="size-full" role="presentation">
        <text
          x="16"
          y="16"
          textAnchor="middle"
          dominantBaseline="central"
          fill="currentColor"
          fontSize="19"
          fontWeight="600"
          letterSpacing="-0.5"
          fontFamily="inherit"
        >
          F
        </text>
      </svg>
    </div>
  );
}
