import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsModal } from "./SettingsModal";
import { PrefsProvider } from "../prefs";
import { mockFetch } from "../test/fixtures";
import type { ModelConfig } from "../types";

const cfg: ModelConfig = { backend: "ollama", model: "qwen3-vl:4b", host: "http://127.0.0.1:11434", model_path: "", device: "GPU", temperature: 0.2, max_tokens: 512, context_tokens: 8192 };
const active = { backend: "ollama", model: "qwen3-vl:4b", simulated: false };

function open(tab: "prefs" | "guide" | "companion" = "prefs") {
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
  test("導讀: the guide language follows the interface language until chosen, then stays", async () => {
    localStorage.setItem("mentor.locale", JSON.stringify("en"));
    open("guide");
    const menu = screen.getByRole("combobox", { name: "Guide language" });
    expect(menu).toHaveTextContent("English");
    await userEvent.click(menu);
    await userEvent.click(screen.getByRole("option", { name: "Chinese" }));
    expect(screen.getByRole("combobox", { name: "Guide language" })).toHaveTextContent("Chinese");
    expect(JSON.parse(localStorage.getItem("mentor.guideLang")!)).toBe("zh");
  });

  test("偏好: switching language re-renders in English and theme pins <html data-theme>", async () => {
    localStorage.setItem("mentor.locale", JSON.stringify("zh-Hant"));
    open();
    await userEvent.click(screen.getByRole("radio", { name: "暗" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    await userEvent.click(screen.getByRole("combobox", { name: "語系" }));
    await userEvent.click(screen.getByRole("option", { name: "English" }));
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
    await screen.findByRole("radio", { name: "選擇 Kuri" });
    expect(screen.getByRole("radio", { name: "選擇 Moyu" }).getAttribute("aria-checked")).toBe("true");
    await userEvent.click(screen.getByRole("radio", { name: "選擇 Kuri" }));
    expect((screen.getByLabelText("模型") as HTMLInputElement).value).toBe("qwen3-vl:2b");
    await userEvent.click(screen.getByRole("button", { name: "儲存變更" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /api/companions")).toBeTruthy());
    const put = calls.find((c) => c.key === "PUT /api/companions")!.body as any;
    expect(put.active).toBe("fox");
    expect(put.companions.owl.model).toBe("qwen3-vl:4b"); // Moyu keeps the settings that were live before
    expect(put.companions.fox.temperature).toBe(0.5);
  });
});

describe("SettingsModal · 遠端模型", () => {
  test("choosing the remote backend shows the server fields; the token is sent once and never shown", async () => {
    localStorage.setItem("mentor.locale", JSON.stringify("zh-Hant"));
    const calls = mockFetch({
      "GET /api/companions": () => ({ json: null }),
      "GET /api/settings": () => ({ json: { config: cfg, active, remote: { url: "", has_token: false } } }),
      "POST /api/settings/check": () => ({ json: { active, health: { ok: true, detail: "ok", available_models: [] } } }),
      "PUT /api/settings/remote": (body) => ({ json: { remote: { url: (body as any).url, has_token: true } } }),
    });
    open("companion");
    await screen.findByRole("radio", { name: "選擇 Kuri" });
    expect(screen.queryByTestId("remote-endpoint")).toBeNull();
    await userEvent.click(screen.getByRole("combobox", { name: "後端" }));
    await userEvent.click(screen.getByRole("option", { name: /遠端/ }));
    await userEvent.type(screen.getByLabelText("伺服器網址"), "https://abc.trycloudflare.com");
    await userEvent.type(screen.getByLabelText("存取 token"), "tok-123");
    await userEvent.click(screen.getByRole("button", { name: "儲存連線" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /api/settings/remote")?.body).toEqual({ url: "https://abc.trycloudflare.com", token: "tok-123" }));
    const token = screen.getByLabelText("存取 token") as HTMLInputElement;
    expect(token.value).toBe("") ;
    expect(token.placeholder).toBe("已設定，留空沿用");
    expect(token.type).toBe("password");
  });
});

describe("SettingsModal · 並行數量", () => {
  test("the parallel setting is saved with the companion's model and clamped to 1–8", async () => {
    localStorage.setItem("mentor.locale", JSON.stringify("zh-Hant"));
    const calls = mockFetch({
      "GET /api/companions": () => ({ json: null }),
      "GET /api/settings": () => ({ json: { config: cfg, active, remote: { url: "", has_token: false } } }),
      "POST /api/settings/check": () => ({ json: { active, health: { ok: true, detail: "ok", available_models: [] } } }),
      "PUT /api/companions": (body) => ({ json: { companions: body, config: (body as any).companions[(body as any).active], active } }),
    });
    open("companion");
    await screen.findByRole("radio", { name: "選擇 Kuri" });
    const input = screen.getByLabelText(/^並行數量/) as HTMLInputElement;
    expect(input.value).toBe("1");
    await userEvent.clear(input);
    await userEvent.type(input, "12");
    expect(input.value).toBe("8");
    await userEvent.click(screen.getByRole("button", { name: "儲存變更" }));
    await waitFor(() => {
      const body = calls.find((c) => c.key === "PUT /api/companions")?.body as any;
      expect(body.companions[body.active].parallel).toBe(8);
    });
  });
});
