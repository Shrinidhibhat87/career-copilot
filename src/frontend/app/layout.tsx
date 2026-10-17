import type { Metadata } from "next";
import { IBM_Plex_Mono, Instrument_Serif, Libre_Franklin } from "next/font/google";

import { AuthGuard } from "@/components/auth-guard";

import "./globals.css";

const displayFont = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-display",
});

const uiFont = Libre_Franklin({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-ui",
});

const monoFont = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Career Copilot",
  description: "A quieter way to read the job market.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${displayFont.variable} ${uiFont.variable} ${monoFont.variable}`}>
      <body>
        <AuthGuard />
        {children}
      </body>
    </html>
  );
}
