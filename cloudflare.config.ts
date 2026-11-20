import { defineConfig, bindings, exports } from "cf/config";

export default defineConfig({
  worker: {
    name: "llm-proxy-gateway",
    entrypoint: "./workers/entry.py",
    compatibilityDate: "2026-11-18",
    compatibilityFlags: ["python_workers", "enable_ctx_exports"],
    workersDev: true,
    env: {
      DB: bindings.d1({ name: "llm-proxy-gateway", id: "874b717a-da99-4501-aaa7-a2f5eed975a5" }),
      RECORDS: bindings.r2({ name: "llm-proxy-gateway" }),
      GATEWAY_STATE: bindings.durableObject({ worker: "llm-proxy-gateway", exportName: "GatewayDurableObject" }),
    },
    exports: {
      GatewayDurableObject: exports.durableObject({ storage: "sqlite" }),
    },
    assets: { runWorkerFirst: ["/api/*", "/v1/*"] },
    observability: {
      enabled: true,
      logs: { enabled: true },
      traces: { enabled: true, headSamplingRate: 0.1 },
    },
    placement: { mode: "smart" },
  },
});
