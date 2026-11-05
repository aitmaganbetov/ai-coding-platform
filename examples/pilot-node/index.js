/** Greeting that escapes HTML so it is safe to render (pilot project). */
export function greet(name) {
  const safe = String(name).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  return `Hello, ${safe}!`;
}
