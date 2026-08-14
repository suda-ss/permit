/** @type {import('next').NextConfig} */
// Both processes run in the same container (see docker-entrypoint.sh), so
// the backend is always reachable on loopback — no cross-container network
// config needed, and nginx only ever sees this app's single exposed port.
const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";

// Deployed at <host>/permits (see nginx/permit-agent.conf) rather than its
// own domain/subdomain — basePath is a build-time constant, so it must
// match the nginx location prefix exactly. Update both together.
const BASE_PATH = "/permits";

const nextConfig = {
  // Traces only the node_modules actually needed into .next/standalone,
  // instead of shipping the whole node_modules tree in the Docker image.
  output: "standalone",
  basePath: BASE_PATH,
  async rewrites() {
    return [
      {
        source: `${BASE_PATH}/api/:path*`,
        destination: `${BACKEND_URL}/api/:path*`,
        basePath: false,
      },
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
