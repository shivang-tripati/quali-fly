import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Providers } from "@/components/providers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

/**
 * The one and only root layout. It owns `<html>` and `<body>` and nothing else.
 *
 * **No shell here.** The application frame lives in `(app)/layout.tsx` and the public
 * auth frame in `(auth)/layout.tsx`, so each group decides what surrounds its pages. Keeping
 * the frame out of the root is what stops a sign-in screen from arriving with a sidebar and
 * a notification bell already asking for permissions.
 *
 * Having a single root layout also matters for navigation: Next.js does a full page reload
 * when two routes have *different* root layouts, and the whole point of the split is that
 * moving between sign-in and the dashboard should not throw away the session's query cache.
 *
 * `Providers` stays at the root because the auth forms need the query client too - they call
 * the same `useLogin` / `useRegister` hooks as the app, and those invalidate cached queries
 * on success.
 */
export const metadata: Metadata = {
  title: "QualiFly",
  description: "AI lead nurturing for Meta ads",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
