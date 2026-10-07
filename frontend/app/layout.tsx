import type { Metadata } from "next";

import { AuthProvider } from "@/lib/auth-context";
import { TopBar } from "./top-bar";
import "./globals.css";
import "./immersive.css";

export const metadata: Metadata = {
  title: "GD Platform",
  description: "Group Discussion platform with agentic AI evaluation — local development",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <TopBar />
          <main className="shell">{children}</main>
        </AuthProvider>
      </body>
    </html>
  );
}
