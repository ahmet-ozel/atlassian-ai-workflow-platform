import type { ReactNode } from "react";

import "./globals.css";
import AppShell from "@/components/AppShell";

export const metadata = {
  title: "Admin Console - AI Bot Platform",
  description:
    "Departman, servis, workflow ve maliyet yönetimi için kontrol paneli.",
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#4f46e5",
};

export default function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="tr">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
