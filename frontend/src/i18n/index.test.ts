import { describe, expect, test } from "bun:test";
import { translate, type Key } from "./index";
import { en } from "./en";
import { enPlurals } from "./en.plurals";
import { zh } from "./zh-Hant";

const placeholders = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort();

describe("English localization", () => {
  test("covers every source key and preserves all placeholders, including repeated ones", () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort());
    for (const key of Object.keys(zh) as Key[]) {
      expect(en[key]?.trim()).toBeTruthy();
      expect(placeholders(en[key]!)).toEqual(placeholders(zh[key]));
    }
    for (const [key, forms] of Object.entries(enPlurals)) {
      for (const form of [forms.one, forms.other]) {
        if (form !== undefined) expect(placeholders(form)).toEqual(placeholders(zh[key as Key]));
      }
    }
  });

  test("uses singular for one and plural for zero or many", () => {
    expect(translate("en", "shelf.pages", { count: 0 })).toBe("0 pages");
    expect(translate("en", "shelf.pages", { count: 1 })).toBe("1 page");
    expect(translate("en", "shelf.pages", { count: 12 })).toBe("12 pages");
    expect(translate("en", "shelf.parts", { count: 1 })).toBe("1 chapter");
    expect(translate("en", "history.reading.title", { total: 1 })).toBe("Guided reading · 1 section");
    expect(translate("en", "reading.unverified.summary", { n: 1 })).toContain("Compare it with");
    expect(translate("en", "reading.unverified.summary", { n: 2 })).toContain("Compare them with");
  });

  test("keeps bare unit labels singular and pluralizes counted units", () => {
    expect(translate("en", "material.unit.page")).toBe("page");
    expect(translate("en", "material.unit.line", { count: 1 })).toBe("line");
    expect(translate("en", "material.unit.line", { count: 4 })).toBe("lines");
    const unit = translate("en", "material.unit.page", { count: 3 });
    expect(translate("en", "material.unitCount", { count: 3, unit })).toBe("3 pages");
    expect(translate("en", "material.range.span", { start: 1, end: 3, unit })).toBe("pages 1–3");
  });

  test("leaves Chinese counts and labels unchanged", () => {
    expect(translate("zh-Hant", "shelf.pages", { count: 1 })).toBe("1 頁");
    expect(translate("zh-Hant", "shelf.pages", { count: 12 })).toBe("12 頁");
    expect(translate("zh-Hant", "material.unit.line", { count: 4 })).toBe("行");
    expect(translate("zh-Hant", "material.range.span", { start: 1, end: 3, unit: "行" })).toBe("第 1–3 行");
  });

  test("preserves sentence fragments, repeated placeholders, and literal user input", () => {
    const sentence = translate("en", "home.empty.before") + translate("en", "home.empty.link") + translate("en", "home.empty.after");
    expect(sentence).toBe("Nothing to read yet? Try the sample lesson to see how a session works.");
    expect(translate("en", "progress.reading.done", { total: 3 })).toBe("Guided reading complete · 3/3");
    expect(translate("en", "side.deleteConfirm", { name: "Notes {count} $&" })).toBe("Delete “Notes {count} $&”?");
    expect(translate("en", "shelf.pages")).toBe("{count} pages");
  });
});
