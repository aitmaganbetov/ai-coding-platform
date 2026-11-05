import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, parseRoute } from "./App";
import { RunTable } from "./ProjectPage";

type Handler = (path: string, init: RequestInit) => { status: number; body?: unknown };

function mockFetch(handler: Handler) {
  const calls: { path: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace("/api/v1", "");
      calls.push({ path, init });
      const { status, body } = handler(path, init);
      return new Response(body === undefined ? null : JSON.stringify(body), { status });
    }),
  );
  return calls;
}

beforeEach(() => {
  sessionStorage.clear();
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("routing", () => {
  it("parses hash routes", () => {
    expect(parseRoute("#/projects/p1")).toEqual({ view: "project", id: "p1" });
    expect(parseRoute("#/runs/r1")).toEqual({ view: "run", id: "r1" });
    expect(parseRoute("#/anything")).toEqual({ view: "projects" });
  });
});

describe("login", () => {
  it("logs in, stores the token for the session and sends it as a bearer token", async () => {
    const calls = mockFetch((path) => {
      if (path === "/auth/login") return { status: 201, body: { token: "aic_test" } };
      if (path === "/me") return { status: 200, body: { username: "alice", is_admin: false } };
      if (path === "/projects")
        return {
          status: 200,
          body: [{ id: "p1", name: "alpha", role: "developer", created_at: "2026-11-05T10:00:00" }],
        };
      return { status: 404, body: { error: { message: "not found" } } };
    });
    render(<App />);
    fireEvent.change(screen.getByLabelText("Пользователь"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "secret password" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByText("alpha")).toBeTruthy();
    expect(sessionStorage.getItem("ai-coding-token")).toBe("aic_test");
    const projects = calls.find((c) => c.path === "/projects");
    expect((projects?.init.headers as Record<string, string>).Authorization).toBe("Bearer aic_test");
    expect(projects?.init.credentials).toBe("omit");
  });

  it("shows the server's error and keeps no token", async () => {
    mockFetch(() => ({ status: 401, body: { error: { message: "invalid username or password" } } }));
    render(<App />);
    fireEvent.change(screen.getByLabelText("Пользователь"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "y" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect((await screen.findByRole("alert")).textContent).toContain("invalid username");
    expect(sessionStorage.getItem("ai-coding-token")).toBeNull();
  });

  it("drops an expired token and returns to the login form", async () => {
    sessionStorage.setItem("ai-coding-token", "aic_old");
    mockFetch(() => ({ status: 401, body: { error: { message: "missing or invalid API token" } } }));
    render(<App />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Войти" })).toBeTruthy());
    expect(sessionStorage.getItem("ai-coding-token")).toBeNull();
  });
});

describe("runs table", () => {
  it("renders states and links", () => {
    render(
      <RunTable
        runs={[
          { id: "01a1-0000-000000000001", state: "COMPLETED", mode: "audit", goal: "audit",
            kind: "audit", requested_by: "alice", created_at: "2026-11-05T10:00:00", finished_at: null },
        ]}
      />,
    );
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe("#/runs/01a1-0000-000000000001");
    expect(screen.getByText("COMPLETED").className).toContain("state-completed");
  });
});
