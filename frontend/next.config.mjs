/** @type {import('next').NextConfig} */
const nextConfig = {
  // Extra development checks (React runs effects twice in dev to surface bugs).
  reactStrictMode: true,
};

// Next.js reads this default export as the app's build/runtime configuration.
export default nextConfig;
