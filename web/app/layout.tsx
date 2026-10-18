import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SignalLog — Reliability signals",
  description: "A local dashboard for check results and reliability trends.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
