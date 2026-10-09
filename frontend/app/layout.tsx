import type { Metadata } from "next";

import { AuthProvider } from "@/lib/auth-context";
import { Sidebar } from "./sidebar";
import "./globals.css";
import "./immersive.css";
import "./refresh.css";
import "./refresh2.css";
import "./refresh3.css";

export const metadata: Metadata = {
  title: "GD Platform",
  description: "Group Discussion platform with agentic AI evaluation — local development",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <div className="app">
            <Sidebar />
            <main className="shell">{children}</main>
          </div>
        </AuthProvider>
      </body>
    </html>
  );
}
