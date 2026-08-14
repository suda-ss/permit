import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Permit Research Agent",
  description: "Chat with the construction permit research agent",
  icons: {
    icon: "/icon.svg",
    shortcut: "/icon.svg",
    apple: "/icon.svg",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
