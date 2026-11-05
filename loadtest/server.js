// Load test of the Self-Hosted server (B-800, NFR-02). Run by loadtest/run_load.py.
//
// Scenarios, concurrently:
//   reads   — steady API traffic (project, runs list, run status) at READ_RATE req/s;
//   runs    — RUNS discovery runs created by RUN_VUS users, each polled to completion;
//   sampler — reads /metrics every 200 ms: jobs executing right now across all workers.
// Thresholds encode the acceptance: no failed reads, p95 latency, every run completes,
// at least MIN_PARALLEL runs executing at the same time.
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL;
const PROJECT = __ENV.PROJECT_ID;
const RUNS = parseInt(__ENV.RUNS || "60", 10);
const RUN_VUS = parseInt(__ENV.RUN_VUS || "30", 10);
const READ_RATE = parseInt(__ENV.READ_RATE || "15", 10);
const DURATION = __ENV.DURATION || "60s";
const MIN_PARALLEL = parseInt(__ENV.MIN_PARALLEL || "10", 10);
const P95_MS = parseInt(__ENV.P95_MS || "500", 10);
const POLL = parseFloat(__ENV.POLL_SECONDS || "1");  // the Web UI polls every 2 s
const auth = {
  headers: { Authorization: `Bearer ${__ENV.TOKEN}`, "Content-Type": "application/json" },
};
const metricsAuth = { headers: { Authorization: `Bearer ${__ENV.METRICS_TOKEN}` } };
const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];

const runDuration = new Trend("run_duration", true);
const runsCompleted = new Counter("runs_completed");
const runsFailed = new Rate("runs_failed");
const parallel = new Trend("parallel_runs");

export const options = {
  scenarios: {
    reads: {
      executor: "constant-arrival-rate", exec: "reads", rate: READ_RATE, timeUnit: "1s",
      duration: DURATION, preAllocatedVUs: 20, maxVUs: 100,
    },
    runs: {
      executor: "shared-iterations", exec: "runs", vus: RUN_VUS, iterations: RUNS,
      maxDuration: "10m",
    },
    sampler: { executor: "constant-vus", exec: "sample", vus: 1, duration: DURATION },
  },
  thresholds: {
    "http_req_failed{scenario:reads}": ["rate<0.01"],
    "http_req_duration{scenario:reads}": [`p(95)<${P95_MS}`],
    runs_failed: ["rate==0"],
    runs_completed: [`count>=${RUNS}`],
    parallel_runs: [`max>=${MIN_PARALLEL}`],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

let lastRun = null;

export function reads() {
  const project = http.get(`${BASE}/api/v1/projects/${PROJECT}`, auth);
  check(project, { "project 200": (r) => r.status === 200 });
  const runs = http.get(`${BASE}/api/v1/projects/${PROJECT}/runs`, auth);
  check(runs, { "runs 200": (r) => r.status === 200 });
  if (runs.status === 200 && runs.json().length) lastRun = runs.json()[0].id;
  if (lastRun) {
    const run = http.get(`${BASE}/api/v1/runs/${lastRun}`, auth);
    check(run, { "run 200": (r) => r.status === 200 });
  }
}

export function runs() {
  const started = Date.now();
  const created = http.post(`${BASE}/api/v1/projects/${PROJECT}/runs`,
    JSON.stringify({ kind: "discover" }), auth);
  if (!check(created, { "run created": (r) => r.status === 201 })) {
    runsFailed.add(1);
    return;
  }
  const id = created.json().id;
  let state = created.json().state;
  while (!TERMINAL.includes(state) && Date.now() - started < 300000) {
    sleep(POLL);
    const r = http.get(`${BASE}/api/v1/runs/${id}`, auth, { tags: { name: "poll" } });
    if (r.status === 200) state = r.json().state;
  }
  const ok = state === "COMPLETED";
  check(state, { "run completed": () => ok });
  runsFailed.add(!ok);
  if (ok) {
    runsCompleted.add(1);
    runDuration.add(Date.now() - started);
  }
}

export function sample() {
  const r = http.get(`${BASE}/metrics`, metricsAuth, { tags: { name: "metrics" } });
  if (r.status === 200) {
    const m = r.body.match(/^aicoding_jobs\{state="leased"\} (\d+)/m);
    parallel.add(m ? parseInt(m[1], 10) : 0);
  }
  sleep(0.2);
}
