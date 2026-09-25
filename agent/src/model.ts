// The Ollama model pi will talk to, through the native /api/chat adapter in ollama-api.ts (thinking off).
import { createModels, createProvider, type Model } from "@earendil-works/pi-ai";
import { OLLAMA_CHAT_API, ollamaChatApi } from "./ollama-api";

export function ollamaModels(host: string, modelId: string, contextTokens: number) {
  const baseUrl = host.replace(/\/+$/, "");
  const model: Model<typeof OLLAMA_CHAT_API> = {
    id: modelId,
    name: `${modelId} (Ollama)`,
    api: OLLAMA_CHAT_API,
    provider: "ollama",
    baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: contextTokens,
    maxTokens: 4096,
  };
  const provider = createProvider({
    id: "ollama",
    name: "Ollama",
    baseUrl,
    // local server, no key; pi still requires an auth declaration
    auth: { apiKey: { name: "Ollama", resolve: async () => ({ auth: { apiKey: "ollama" } }) } },
    models: [model],
    api: ollamaChatApi(),
  });
  const models = createModels();
  models.setProvider(provider);
  return { models, model: models.getModel("ollama", modelId)! };
}
