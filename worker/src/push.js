import { createApp } from "./index.js";
import { sendSyncPing } from "./jobs.js";

const app = createApp();

export default {
  fetch(request, env, ctx) {
    return app.fetch(request, env, ctx);
  },
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(sendSyncPing(env));
  },
};
