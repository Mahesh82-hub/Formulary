import type { Metadata } from "next";
import { Toaster } from "sonner";

import { AppProviders } from "@/components/app-providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Formulary",
  description: "A careful pharmaceutical research assistant.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <AppProviders>{children}</AppProviders>
        <Toaster
          richColors
          position="top-center"
          toastOptions={{
            style: {
              background: "var(--surface)",
              borderColor: "var(--border)",
              color: "var(--foreground)",
            },
          }}
        />
      </body>
    </html>
  );
}
