/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Fail the build on type or lint errors: the dashboard is generated against
  // the backend OpenAPI schema, and a drifting contract must not ship green.
  typescript: { ignoreBuildErrors: false },
  eslint: { ignoreDuringBuilds: false },
  // The browser talks to the API directly (CORS allowlisted server-side);
  // no rewrite proxy, so the deployed origin split stays explicit.
  env: {},
  // Playwright drives the dev server over 127.0.0.1 while it's addressed as
  // localhost internally; harmless outside dev (this option is a no-op for
  // `next build`/`next start`).
  allowedDevOrigins: ["127.0.0.1"],
  // Response headers for the hosted console (docs/deployment.md). These are
  // hardening around the access layer, never a substitute for it: the
  // security boundary is the identity-aware proxy in front of the
  // deployment (ADR-017, ADR-026). `frame-ancestors 'none'` matters most —
  // the console performs privileged approvals, so it must not be
  // embeddable, and the proxy's login page must not be framed either.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
