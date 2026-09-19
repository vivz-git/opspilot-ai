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
};

export default nextConfig;
