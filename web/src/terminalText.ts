// Text helpers of the browser terminal: links on screen and the clipboard.

const URL_RE = /https?:\/\/[^\s"'<>`]+/g;

/** URLs on screen, rejoined where tmux broke them across rows (a URL has no spaces:
 * a row filled to the last column continues on the next one). */
export function findLinks(rows: string[], cols: number): string[] {
  const joined: string[] = [];
  for (const row of rows) {
    const previous = joined.length > 0 ? joined[joined.length - 1] : undefined;
    if (previous !== undefined && previous.length % cols === 0 && previous.length > 0 && row && !row.startsWith(" ")) {
      joined[joined.length - 1] = previous + row;
    } else {
      joined.push(row);
    }
  }
  return joined.flatMap((line) => line.match(URL_RE) ?? []).map((url) => url.replace(/[.,;:)\]]+$/, ""));
}

/** Copy to the clipboard, also on plain-HTTP sites (no Clipboard API there). */
export function copyText(text: string): boolean {
  if (window.isSecureContext && navigator.clipboard) {
    void navigator.clipboard.writeText(text);
    return true;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}
