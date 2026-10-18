/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The platform's build (scripts/start_platform.ps1) has its own folder: Next.js
  // inlines NEXT_PUBLIC_API_URL at build time, and the standalone build's differs.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
