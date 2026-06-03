// Minimal Next.js 14 config for the admin dashboard.
// See: https://nextjs.org/docs/app/api-reference/next-config-js

/** @type {import('next').NextConfig} */
const nextConfig = {
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
