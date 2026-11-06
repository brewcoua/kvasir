import { defineConfig } from "@playwright/test";

const MOCK = "http://localhost:5179";
const SERVICE = "http://127.0.0.1:8765";

// `mock` drives the page against the fixtures. `service` checks the build as the service serves it,
// with a configuration that points nowhere, since no test here starts a run on it.
export default defineConfig({
  testDir: "e2e",
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? "github" : "list",
  use: { trace: "retain-on-failure" },
  projects: [
    { name: "mock", testMatch: "mock.spec.ts", use: { baseURL: MOCK } },
    { name: "service", testMatch: "service.spec.ts", use: { baseURL: SERVICE } },
  ],
  webServer: [
    {
      command: "pnpm dev:mock --port 5179 --strictPort",
      url: MOCK,
      // Slow enough that a started run is still going when the test looks at it.
      env: { MOCK_SPEED: "20" },
      reuseExistingServer: !process.env.CI,
    },
    {
      // Nothing stored in its data directory, so it starts as a first start does.
      command: "pnpm build && cargo run -q --manifest-path ../../Cargo.toml -p kvasir -- serve",
      url: `${SERVICE}/healthz`,
      env: {
        KVASIR_PROVIDER_UNUSED_API_BASE: "http://127.0.0.1:9",
        KVASIR_DATA_DIR: "test-results/data",
        KVASIR_TOKEN: "e2e-token",
        KVASIR_HOST: "127.0.0.1",
        KVASIR_PORT: "8765",
      },
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
