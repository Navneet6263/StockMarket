import type { Metadata } from "next";
import { Inter } from "next/font/google";
import type { ReactNode } from "react";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], display: "swap", variable: "--font-sans" });

export const metadata: Metadata = {
  title: "Stock Intelligence",
  description: "Smart stock scanner with AI-powered picks",
};

import AuthOverlay from "./components/AuthOverlay";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className={inter.variable}>
        <AuthOverlay>{children}</AuthOverlay>
      </body>
    </html>
  );
}
