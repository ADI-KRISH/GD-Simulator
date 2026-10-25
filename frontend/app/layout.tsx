import type { Metadata } from "next";  // page metadata type

import { AuthProvider } from "@/lib/auth-context";  // makes the signed-in user available everywhere
import { Sidebar } from "./sidebar";  // left navigation
import "./globals.css";  // base styles
import "./immersive.css";  // styles for the live practice/room screens
import "./refresh.css";  // design refresh layer 1
import "./refresh2.css";  // design refresh layer 2
import "./refresh3.css";  // design refresh layer 3
import "./theme.css";  // dark theme, loaded last so its tokens win

/** Page title and description for every route. */
export const metadata: Metadata = {
  title: "GD Platform",
  description: "Group Discussion platform with agentic AI evaluation — local development",
};

/** App shell: auth provider, sidebar and the page content area. */
/** Runs before first paint so the saved theme never flashes the wrong colours. */
const THEME_SCRIPT = `try{var t=localStorage.getItem("gd-theme");if(t==="dark"||t==="light")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
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
