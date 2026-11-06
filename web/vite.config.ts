import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";
import { mock } from "./mock/plugin.ts";

// `dev` proxies the API to a kvasir on its default port. `dev:mock` answers it from the fixtures
// instead, so a view can be worked on without a model provider, a search engine or a run.
export default defineConfig(({ mode }) => ({
  plugins: [svelte(), mode === "mock" && mock()],
  server: {
    proxy: mode === "mock" ? undefined : { "/v1": "http://localhost:8080" },
  },
  resolve: process.env.VITEST ? { conditions: ["browser"] } : undefined,
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
}));
