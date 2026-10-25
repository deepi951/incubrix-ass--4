
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const API_PROXY_TARGET =
    env.API_PROXY_TARGET || "http://127.0.0.1:8001";

  return {
    plugins: [react()],
    server: {
      proxy: {
        "/api/v1": {
          target: API_PROXY_TARGET,
          changeOrigin: true,
        },
        "/connector": {
          target: API_PROXY_TARGET,
          changeOrigin: true,
        },
        "/health": {
          target: API_PROXY_TARGET,
          changeOrigin: true,
        },
        "/ready": {
          target: API_PROXY_TARGET,
          changeOrigin: true,
        },
      },
    },
  };
});