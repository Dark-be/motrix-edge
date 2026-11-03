import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Edge Web Console：独立 dev server（CORS 已由 Edge 放开）；默认直连 http://localhost:8000，
// 同时提供 /v1 代理以便 baseUrl 留空时同源访问。
export default defineConfig({
    plugins: [react(), tailwindcss()],
    server: {
        port: 5173,
        proxy: {
            "/api": {
                target: "http://localhost:5174",
                changeOrigin: true,
            },
            "/v1": {
                target: "http://localhost:8000",
                changeOrigin: true,
            },
        },
    },
});
