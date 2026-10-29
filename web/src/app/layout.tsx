import type { Metadata } from "next";

import { Providers } from "@/app/providers";
import "@/styles/globals.css";

export const metadata: Metadata = {
  title: "TAZAMA — Network Care Intelligence",
  description:
    "Correlate customer complaints with network KPIs, explain incidents, and act with evidence. Synthetic data only.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
