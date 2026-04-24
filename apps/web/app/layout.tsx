import type { Metadata } from "next";
import { IBM_Plex_Mono, Space_Grotesk } from "next/font/google";
import type { ReactNode } from "react";
import "./globals.css";

const space = Space_Grotesk({ subsets: ["latin"], display: "swap", variable: "--font-sans" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], display: "swap", weight: ["400", "500"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: "Market Intelligence Terminal",
  description: "Real-time market scanner, explainable prediction engine, and stock intelligence dashboard.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className={`${space.variable} ${mono.variable}`}>{children}</body>
    </html>
  );
}
