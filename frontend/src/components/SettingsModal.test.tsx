import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsModal } from "./SettingsModal";
import { PrefsProvider } from "../prefs";
import { mockFetch } from "../test/fixtures";
import type { ModelConfig } from "../types";

const cfg: ModelConfig = { backend: "ollama", model: "qwen3-vl:4b", host: "http://127.0.0.1:11434", model_path: "", device: "GPU", temperature: 0.2, max_tokens: 512, context_tokens: 8192 };
const active = { backend: "ollama", model: "qwen3-vl:4b", simulated: false };

function open(tab: "prefs" | "companion" = "prefs") {
  let closed = 0;
  render(
    <PrefsProvider>
      <SettingsModal initialTab={tab} onClose={() => closed++} onBackendChanged={() => {}} />
    </PrefsProvider>,
  );
  return { closed: () => closed };
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("SettingsModal", () => {
  test("偏好: switching language re-renders in English and theme pins <html data-theme>", async () => {
    localStorage.setItem("mentor.locale", JSON.stringify("zh-Hant"));
    open();
    await userEvent.click(screen.getByRole("radio", { name: "暗" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "語系" }), "en");
    expect(screen.getByRole("tab", { name: "Preferences" })).toBeTruthy();
    expect(document.documentElement.lang).toBe("en");
    await userEvent.click(screen.getByRole("radio", { name: "System" }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  test("Escape closes", async () => {
    const m = open();
    await userEvent.keyboard("{Escape}");
    expect(m.closed()).toBe(1);
  });

  test("伴讀: seeds companions from current settings, switching companion saves its model", async () => {
    localStorage.setItem("mentor.locale", JSON.stringify("zh-Hant"));
    const calls = mockFetch({
      "GET /api/companions": () => ({ json: null }),
      "GET /api/settings": () => ({ json: { config: cfg, active } }),
      "POST /api/settings/check": () => ({ json: { active, health: { ok: true, detail: "ok", available_models: ["qwen3-vl:2b"] } } }),
      "PUT /api/companions": (body) => ({ json: { companions: body, config: (body as any).companions.fox, active: { ...active, model: "qwen3-vl:2b" } } }),
    });
    open("companion");
    await screen.findByRole("radio", { name: "選擇 小栗" });
    expect(screen.getByRole("radio", { name: "選擇 墨羽" }).getAttribute("aria-checked")).toBe("true");
    await userEvent.click(screen.getByRole("radio", { name: "選擇 小栗" }));
    expect((screen.getByLabelText("模型") as HTMLInputElement).value).toBe("qwen3-vl:2b");
    await userEvent.click(screen.getByRole("button", { name: "儲存變更" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /api/companions")).toBeTruthy());
    const put = calls.find((c) => c.key === "PUT /api/companions")!.body as any;
    expect(put.active).toBe("fox");
    expect(put.companions.owl.model).toBe("qwen3-vl:4b"); // 墨羽 keeps the settings that were live before
    expect(put.companions.fox.temperature).toBe(0.5);
  });
});
