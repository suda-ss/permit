import type { Metadata } from "next";
import "./globals.css";

const appBasePath = (process.env.NEXT_PUBLIC_APP_BASE_PATH || "/permits").replace(/\/$/, "");
const iconPath = `${appBasePath}/icon.svg`;

export const metadata: Metadata = {
  title: "Permit Research Agent",
  description: "Chat with the construction permit research agent",
  icons: {
    icon: iconPath,
    shortcut: iconPath,
    apple: iconPath,
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
