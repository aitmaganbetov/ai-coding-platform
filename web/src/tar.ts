// A project folder chosen in the browser, packed as an uncompressed POSIX (pax) tar.
// The Blob references the File objects: nothing is read into memory before the upload.

const BLOCK = 512;
const encoder = new TextEncoder();

// Not uploaded: dependencies and caches (rebuilt by the tools), the local git
// repository (the server starts a fresh one), the local platform state, OS litter.
const SKIPPED_DIRS = new Set([
  ".git",
  "node_modules",
  ".venv",
  "venv",
  "__pycache__",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  ".tox",
  ".next",
  ".nuxt",
  ".gradle",
  ".ai-coding",
]);
const SKIPPED_FILES = new Set([".DS_Store", "Thumbs.db", "desktop.ini"]);
const KEPT = new Set([".ai-coding/config.yml"]); // the project's platform configuration

export interface FolderEntry {
  path: string;
  file: File;
}

export interface FolderSelection {
  folder: string; // the chosen folder's own name
  entries: FolderEntry[];
  bytes: number;
  skipped: Map<string, number>; // skipped directory name → files in it
}

/** The files of a folder picked with <input webkitdirectory>, minus the skipped ones. */
export function selectFolder(files: ArrayLike<File>): FolderSelection {
  const entries: FolderEntry[] = [];
  const skipped = new Map<string, number>();
  let folder = "";
  let bytes = 0;
  for (const file of Array.from(files)) {
    const parts = (file.webkitRelativePath || file.name).split("/").filter(Boolean);
    if (parts.length > 1) folder ||= parts[0] ?? "";
    const rest = parts.length > 1 ? parts.slice(1) : parts;
    const path = rest.join("/");
    const dir = rest.slice(0, -1).find((p) => SKIPPED_DIRS.has(p));
    if (dir !== undefined && !KEPT.has(path)) {
      skipped.set(dir, (skipped.get(dir) ?? 0) + 1);
      continue;
    }
    if (SKIPPED_FILES.has(rest[rest.length - 1] ?? "")) continue;
    entries.push({ path, file });
    bytes += file.size;
  }
  entries.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return { folder, entries, bytes, skipped };
}

function field(block: Uint8Array, text: string, offset: number, length: number): void {
  block.set(encoder.encode(text).subarray(0, length), offset);
}

function octal(value: number, length: number): string {
  return `${value.toString(8).padStart(length - 1, "0")}\0`;
}

function header(name: string, size: number, mtime: number, type: "0" | "x"): Uint8Array {
  const block = new Uint8Array(BLOCK);
  // The real (UTF-8, any length) path is in the pax record; this one is only a fallback.
  field(block, name.replace(/[^\x20-\x7e]/g, "_").slice(-99), 0, 100);
  field(block, octal(0o644, 8), 100, 8);
  field(block, octal(0, 8), 108, 8);
  field(block, octal(0, 8), 116, 8);
  field(block, octal(size, 12), 124, 12);
  field(block, octal(mtime, 12), 136, 12);
  field(block, "        ", 148, 8); // checksum is computed with spaces here
  field(block, type, 156, 1);
  field(block, "ustar\0", 257, 6);
  field(block, "00", 263, 2);
  let sum = 0;
  for (const byte of block) sum += byte;
  field(block, `${sum.toString(8).padStart(6, "0")}\0 `, 148, 8);
  return block;
}

// "<length> path=<value>\n", the length counting its own digits.
function paxRecord(key: string, value: string): Uint8Array {
  const body = encoder.encode(` ${key}=${value}\n`).length;
  let total = body + String(body).length;
  while (total !== body + String(total).length) total = body + String(total).length;
  return encoder.encode(`${total} ${key}=${value}\n`);
}

function padding(size: number): Uint8Array {
  return new Uint8Array((BLOCK - (size % BLOCK)) % BLOCK);
}

export function buildTar(entries: FolderEntry[]): Blob {
  const parts: BlobPart[] = [];
  for (const { path, file } of entries) {
    const mtime = Math.max(0, Math.floor(file.lastModified / 1000));
    const pax = paxRecord("path", path);
    parts.push(header(`PaxHeader/${path}`, pax.length, mtime, "x"), pax, padding(pax.length));
    parts.push(header(path, file.size, mtime, "0"), file, padding(file.size));
  }
  parts.push(new Uint8Array(BLOCK * 2)); // end of archive
  return new Blob(parts, { type: "application/x-tar" });
}
