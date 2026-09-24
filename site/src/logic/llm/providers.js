// Phase 0, 2026-09-23, Origin https://ajeenckya5.github.io
// POST preflight Access-Control-Allow-Origin:
//   Gemini reflected the origin, Groq *, OpenAI reflected, Anthropic * with
//   anthropic-dangerous-direct-browser-access, OpenRouter *.
// Ollama is localhost and is offered only in Mac mode.

export const PROVIDERS = [
  {
    id: "gemini",
    label: "Gemini",
    browser: true,
    terms: "https://ai.google.dev/gemini-api/docs/pricing",
    termsLabel: "Gemini pricing and free tier",
  },
  {
    id: "groq",
    label: "Groq",
    browser: true,
    terms: "https://groq.com/terms-of-use/",
    termsLabel: "Groq terms",
  },
  {
    id: "openai",
    label: "OpenAI",
    browser: true,
    terms: "https://openai.com/policies/api-data-usage-policies/",
    termsLabel: "OpenAI API data usage",
  },
  {
    id: "anthropic",
    label: "Anthropic",
    browser: true,
    terms: "https://www.anthropic.com/legal/commercial-terms",
    termsLabel: "Anthropic commercial terms",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    browser: true,
    terms: "https://openrouter.ai/terms",
    termsLabel: "OpenRouter terms",
  },
  {
    id: "ollama",
    label: "Ollama on this Mac",
    browser: false,
    terms: "https://ollama.com/legal/terms",
    termsLabel: "Ollama terms",
  },
];

const BY_ID = new Map(PROVIDERS.map((provider) => [provider.id, provider]));

export function providerById(id) {
  return BY_ID.get(id) || null;
}

export function providersForMode(mac) {
  return PROVIDERS.filter((provider) => provider.browser || mac);
}

export function isMacHost(hostname = "") {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
}

const REQUIREMENT = {
  type: "object",
  additionalProperties: false,
  properties: {
    text: { type: "string" },
    type: { type: "string", enum: ["must", "nice"] },
    status: { type: "string", enum: ["met", "partial", "missing"] },
    evidence_quote: { type: "string" },
    note: { type: "string" },
  },
  required: ["text", "type", "status", "evidence_quote", "note"],
};

const SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    jobs: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        properties: {
          job_id: { type: "string" },
          requirements: { type: "array", items: REQUIREMENT },
          years_required: { type: "number" },
          seniority_fit: { type: "string", enum: ["under", "match", "over"] },
          role_fit: { type: "number" },
          dealbreakers: { type: "array", items: { type: "string" } },
          llm_overall: { type: "number" },
          summary: { type: "string" },
        },
        required: [
          "job_id", "requirements", "years_required", "seniority_fit", "role_fit",
          "dealbreakers", "llm_overall", "summary",
        ],
      },
    },
  },
  required: ["jobs"],
};

export function freeTierFor(providerId, model = "") {
  const name = String(model || "").toLowerCase();
  if (providerId === "gemini") {
    if (name.includes("lite")) return { rpm: 15, rpd: 1000 };
    if (name.includes("pro")) return { rpm: 5, rpd: 100 };
    return { rpm: 10, rpd: 250 };
  }
  if (providerId === "groq") {
    if (name.includes("8b")) return { rpm: 30, rpd: 14400 };
    if (name.includes("70b") || name.includes("versatile")) return { rpm: 2, rpd: 1000 };
    return { rpm: 30, rpd: 1000 };
  }
  if (providerId === "openai") return { rpm: 3, rpd: 50 };
  if (providerId === "anthropic") return { rpm: 5, rpd: 50 };
  if (providerId === "openrouter") return { rpm: 16, rpd: 50 };
  return { rpm: 30, rpd: 5000 };
}

function chatBody(model, prompt) {
  return {
    model,
    temperature: 0,
    messages: [
      { role: "system", content: prompt.system },
      { role: "user", content: prompt.user },
    ],
  };
}

export function buildProviderRequest(providerId, { model, prompt, apiKey = "" }) {
  const provider = providerById(providerId);
  if (!provider) throw new Error("Choose a provider.");
  if (providerId === "gemini") {
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;
    return {
      url,
      headers: {
        "content-type": "application/json",
        ...(apiKey ? { "x-goog-api-key": apiKey } : {}),
      },
      body: {
        contents: [{ role: "user", parts: [{ text: `${prompt.system}\n\n${prompt.user}` }] }],
        generationConfig: {
          temperature: 0,
          responseMimeType: "application/json",
          responseSchema: SCHEMA,
        },
      },
    };
  }
  if (providerId === "anthropic") {
    return {
      url: "https://api.anthropic.com/v1/messages",
      headers: {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
        ...(apiKey ? { "x-api-key": apiKey } : {}),
      },
      body: {
        model,
        max_tokens: 4096,
        temperature: 0,
        system: [
          { type: "text", text: prompt.system },
          {
            type: "text",
            text: `<untrusted_resume>\n${prompt.resume || ""}\n</untrusted_resume>`,
            cache_control: { type: "ephemeral" },
          },
        ],
        messages: [{ role: "user", content: prompt.user.replace(/<untrusted_resume>[\s\S]*?<\/untrusted_resume>\n*/ , "") }],
        tools: [{
          name: "score_jobs",
          description: "Return requirement evidence for the jobs.",
          input_schema: SCHEMA,
        }],
        tool_choice: { type: "tool", name: "score_jobs" },
      },
    };
  }
  if (providerId === "ollama") {
    return {
      url: "http://127.0.0.1:11434/api/chat",
      headers: { "content-type": "application/json" },
      body: {
        model,
        stream: false,
        format: SCHEMA,
        options: { temperature: 0 },
        messages: chatBody(model, prompt).messages,
      },
    };
  }
  const roots = {
    groq: "https://api.groq.com/openai/v1/chat/completions",
    openai: "https://api.openai.com/v1/chat/completions",
    openrouter: "https://openrouter.ai/api/v1/chat/completions",
  };
  const format = providerId === "openai"
    ? { type: "json_schema", json_schema: { name: "scores", strict: true, schema: SCHEMA } }
    : { type: "json_object" };
  return {
    url: roots[providerId],
    headers: {
      "content-type": "application/json",
      ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}),
      ...(providerId === "openrouter" ? { "x-title": "Job Autopilot" } : {}),
    },
    body: { ...chatBody(model, prompt), response_format: format },
  };
}

export function modelsRequest(providerId, apiKey = "") {
  if (providerId === "gemini") {
    return {
      url: "https://generativelanguage.googleapis.com/v1beta/models",
      headers: apiKey ? { "x-goog-api-key": apiKey } : {},
    };
  }
  if (providerId === "anthropic") {
    return {
      url: "https://api.anthropic.com/v1/models",
      headers: {
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
        ...(apiKey ? { "x-api-key": apiKey } : {}),
      },
    };
  }
  if (providerId === "ollama") {
    return { url: "http://127.0.0.1:11434/api/tags", headers: {} };
  }
  const roots = {
    groq: "https://api.groq.com/openai/v1/models",
    openai: "https://api.openai.com/v1/models",
    openrouter: "https://openrouter.ai/api/v1/models",
  };
  return {
    url: roots[providerId],
    headers: apiKey ? { authorization: `Bearer ${apiKey}` } : {},
  };
}

export function parseModelList(providerId, payload) {
  const rows = providerId === "ollama"
    ? (payload?.models || [])
    : (payload?.data || payload?.models || []);
  return rows.map((row) => {
    const name = row.id || row.name || "";
    return String(name).replace(/^models\//, "");
  }).filter(Boolean);
}

export function readModelText(providerId, payload) {
  if (providerId === "anthropic") {
    const tool = (payload?.content || []).find((part) => part.type === "tool_use");
    return JSON.stringify(tool?.input || {});
  }
  if (providerId === "gemini") {
    return payload?.candidates?.[0]?.content?.parts?.map((part) => part.text || "").join("\n") || "";
  }
  if (providerId === "ollama") {
    return payload?.message?.content || "";
  }
  return payload?.choices?.[0]?.message?.content || "";
}
