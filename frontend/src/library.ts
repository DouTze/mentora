import { api, RequestError } from "./api";
import { notify } from "./components/Toaster";
import type { Material } from "./types";

type T = (key: never, vars?: Record<string, string | number>) => string;

/** Tell the person a file is already in the library (the server refused it as the same file), with a way to open it. */
export function noticeDuplicate(m: Pick<Material, "id" | "title">, t: T, open: (id: string) => void): void {
  notify({
    tone: "warn",
    message: t("library.duplicate" as never, { title: m.title }),
    action: { label: t("library.open" as never), run: () => open(m.id) },
  });
}

/**
 * Import files one after another. A file already in the library is not imported again: a notice names the book it
 * already is (with a way to open it) and the next file goes on. Other errors stop the import. Returns the last book
 * imported, if any.
 */
export async function importFiles(files: File[], categoryId: string | null, t: T, open: (id: string) => void): Promise<Material | null> {
  let last: Material | null = null;
  for (const f of files) {
    try {
      last = await api.importMaterial(f, categoryId);
    } catch (e) {
      const dup = e instanceof RequestError && e.body.code === "duplicate" ? (e.body as unknown as { material?: Material }).material : undefined;
      if (!dup) throw e;
      noticeDuplicate(dup, t, open);
    }
  }
  return last;
}
