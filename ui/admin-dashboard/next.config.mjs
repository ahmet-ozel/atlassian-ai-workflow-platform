// Minimal Next.js 14 config for the admin-dashboard scaffold.
// See: https://nextjs.org/docs/app/api-reference/next-config-js

/** @type {import('next').NextConfig} */
const nextConfig = {
  // production-hardening Requirement 13.5 — security headers applied to
  // every response served by the Next.js server (XSS, clickjacking,
  // MIME-sniffing mitigation).
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-XSS-Protection", value: "1; mode=block" },
        ],
      },
    ];
  },
};

export default nextConfig;
