// Minimal Next.js 14 config for the admin dashboard.
// See: https://nextjs.org/docs/app/api-reference/next-config-js

const adminApiInternalUrl = process.env.ADMIN_DASHBOARD_API_INTERNAL_URL?.replace(/\/$/, "");

if (!adminApiInternalUrl) {
  throw new Error("ADMIN_DASHBOARD_API_INTERNAL_URL is required");
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/admin/:path*",
        destination: `${adminApiInternalUrl}/admin/:path*`,
      },
      {
        source: "/api/:path*",
        destination: `${adminApiInternalUrl}/api/:path*`,
      },
      {
        source: "/healthz",
        destination: `${adminApiInternalUrl}/healthz`,
      },
    ];
  },

  // Security headers applied to
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
