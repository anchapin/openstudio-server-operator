#!/usr/bin/env node
/**
 * wave-planner.test.js — Regression tests for wave-planner.js
 *
 * Run with: node wave-planner.test.js
 */

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const wavePlannerPath = path.join(__dirname, "wave-planner.js");

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`  \u2713 ${name}`);
    passed++;
  } catch (err) {
    console.log(`  \u2717 ${name}`);
    console.log(`    Error: ${err.message}`);
    failed++;
  }
}

function runPlanner(stdinData) {
  return new Promise((resolve, reject) => {
    const proc = spawn("node", [wavePlannerPath], {
      cwd: __dirname,
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("close", (code) => {
      if (code !== 0 && stderr) {
        reject(new Error(stderr));
      } else {
        resolve({ stdout, code });
      }
    });

    if (stdinData) {
      proc.stdin.write(JSON.stringify(stdinData));
      proc.stdin.end();
    } else {
      proc.stdin.end();
    }
  });
}

function runPlannerDryRun(stdinData) {
  return new Promise((resolve, reject) => {
    const proc = spawn("node", [wavePlannerPath, "--dry-run"], {
      cwd: __dirname,
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("close", (code) => {
      if (code !== 0 && stderr) {
        reject(new Error(stderr));
      } else {
        resolve({ stdout, code });
      }
    });

    if (stdinData) {
      proc.stdin.write(JSON.stringify(stdinData));
      proc.stdin.end();
    } else {
      proc.stdin.end();
    }
  });
}

console.log("\n=== wave-planner.js Regression Tests ===\n");

// ============================================================
// Integration tests (running the actual script)
// ============================================================
console.log("Integration tests:");

test("plans waves for multiple issues", async () => {
  const issues = [
    { number: 1, title: "Issue 1", body: "internal/file1.go:10", state: "open", labels: [] },
    { number: 2, title: "Issue 2", body: "internal/file2.go:20", state: "open", labels: [] },
    { number: 3, title: "Issue 3", body: "internal/file3.go:30", state: "open", labels: [] },
    { number: 4, title: "Issue 4", body: "internal/file4.go:40", state: "open", labels: [] },
  ];

  const { stdout } = await runPlanner({ issues });
  const plan = JSON.parse(stdout);

  if (plan.total_issues !== 4) throw new Error(`Expected 4 issues, got ${plan.total_issues}`);
  if (plan.total_waves < 1) throw new Error("Should have at least one wave");
  if (plan.waves.length < 1) throw new Error("Should have waves array with content");
});

test("filters out closed issues", async () => {
  const issues = [
    { number: 1, title: "Open", body: "internal/file1.go", state: "open", labels: [] },
    { number: 2, title: "Closed", body: "internal/file2.go", state: "closed", labels: [] },
  ];

  const { stdout } = await runPlanner({ issues });
  const plan = JSON.parse(stdout);

  if (plan.total_issues !== 1) throw new Error(`Expected 1 issue after filter, got ${plan.total_issues}`);
  if (plan._meta.filtered_closed !== 1) throw new Error(`Expected 1 filtered, got ${plan._meta.filtered_closed}`);
});

test("returns empty plan for empty input", async () => {
  const { stdout } = await runPlanner([]);
  const plan = JSON.parse(stdout);

  if (plan.total_issues !== 0) throw new Error(`Expected 0 issues, got ${plan.total_issues}`);
  if (plan.total_waves !== 0) throw new Error("Should have 0 waves");
  if (plan.waves.length !== 0) throw new Error("Should have empty waves array");
});

test("respects MAX_PER_WAVE limit", async () => {
  const issues = [
    { number: 1, title: "Issue 1", body: "internal/file1.go", state: "open", labels: [] },
    { number: 2, title: "Issue 2", body: "internal/file2.go", state: "open", labels: [] },
    { number: 3, title: "Issue 3", body: "internal/file3.go", state: "open", labels: [] },
    { number: 4, title: "Issue 4", body: "internal/file4.go", state: "open", labels: [] },
    { number: 5, title: "Issue 5", body: "internal/file5.go", state: "open", labels: [] },
  ];

  const { stdout } = await runPlanner({ issues });
  const plan = JSON.parse(stdout);

  for (const wave of plan.waves) {
    if (wave.issues.length > 3) {
      throw new Error(`Wave should have at most 3 issues, got ${wave.issues.length}`);
    }
  }
});

test("handles issues sharing files in same wave", async () => {
  const issues = [
    { number: 1, title: "Issue 1", body: "cmd/nexus/main.go", state: "open", labels: [] },
    { number: 2, title: "Issue 2", body: "cmd/nexus/main.go also needs changes", state: "open", labels: [] },
    { number: 3, title: "Issue 3", body: "Different file", state: "open", labels: [] },
  ];

  const { stdout } = await runPlanner({ issues });
  const plan = JSON.parse(stdout);

  // Issues 1 and 2 share cmd/nexus/main.go, so they should be in the same wave
  // But since MAX_PER_WAVE is 3, they could all be in one wave
  const issue1Wave = plan.waves.find((w) => w.issues.some((i) => i.number === 1));
  const issue2Wave = plan.waves.find((w) => w.issues.some((i) => i.number === 2));

  if (!issue1Wave || !issue2Wave) throw new Error("Both issues should be assigned to a wave");
});

test("--dry-run shows affected_files without wave planning", async () => {
  const issues = [
    { number: 1, title: "Issue 1", body: "Fix cmd/nexus/main.go:10", state: "open", labels: [] },
    { number: 2, title: "Issue 2", body: "Check internal/auth/auth.go", state: "open", labels: [] },
  ];

  const { stdout } = await runPlannerDryRun({ issues });
  const result = JSON.parse(stdout);

  if (result._meta.mode !== "dry-run") throw new Error("Should be in dry-run mode");
  if (!result.issues) throw new Error("Should have issues array");
  if (result.issues.length !== 2) throw new Error("Should have 2 issues");

  // In dry-run, issues should have affected_files but not wave assignments
  const issue1 = result.issues.find((i) => i.number === 1);
  if (!issue1.affected_files || issue1.affected_files.length === 0) {
    throw new Error("Issue 1 should have affected_files extracted");
  }
  if (issue1.has_known_deps !== true) throw new Error("Issue 1 should have known deps");
});

test("--dry-run includes HIGH_COLLISION_FILES", async () => {
  const issues = [
    { number: 1, title: "Issue 1", body: "General issue with no specific files", state: "open", labels: [] },
  ];

  const { stdout } = await runPlannerDryRun({ issues });
  const result = JSON.parse(stdout);

  const issue1 = result.issues.find((i) => i.number === 1);
  if (!issue1.affected_files.includes("cmd/nexus/main.go")) {
    throw new Error("Should include cmd/nexus/main.go from HIGH_COLLISION_FILES");
  }
  if (!issue1.affected_files.includes("cmd/nexus/main_test.go")) {
    throw new Error("Should include cmd/nexus/main_test.go from HIGH_COLLISION_FILES");
  }
});

test("shows help with --help flag", async () => {
  return new Promise((resolve, reject) => {
    const proc = spawn("node", [wavePlannerPath, "--help"], {
      cwd: __dirname,
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`--help exited with code ${code}`));
      } else if (!stdout.includes("wave-planner.js")) {
        reject(new Error("--help output missing expected content"));
      } else if (!stdout.includes("--dry-run")) {
        reject(new Error("--help should document --dry-run flag"));
      } else {
        resolve();
      }
    });
  });
});

// ============================================================
// Issue #369: label-overlap heuristic, MAX_UNIQUE_DEPS_PER_WAVE cap,
//             and --no-stacking flag. These tests use spawnSync via a
//             temporary file argument so the assertions actually run
//             (the async spawn+stdin pattern has a race condition
//             against /dev/stdin that can swallow real failures).
// ============================================================
console.log("\nIssue #369 heuristics:");

function runPlannerSync(data, args = []) {
  const tmpFile = "/tmp/_wave_planner_test_input.json";
  fs.writeFileSync(tmpFile, JSON.stringify(data || {}));
  try {
    const proc = spawnSync("node", [wavePlannerPath, tmpFile, ...args], {
      cwd: __dirname,
      encoding: "utf8",
    });
    if (proc.status !== 0) {
      throw new Error(`Planner exited with ${proc.status}: ${proc.stderr}`);
    }
    return proc.stdout;
  } finally {
    if (fs.existsSync(tmpFile)) fs.unlinkSync(tmpFile);
  }
}

function waveOf(plan, issueNumber) {
  return plan.waves.find((w) => w.issues.some((i) => i.number === issueNumber));
}

test("label-overlap heuristic splits issues that share a label (#369)", () => {
  const issues = [
    { number: 1, title: "A", body: "different/fileA.py:10", state: "open", labels: ["observability"] },
    { number: 2, title: "B", body: "different/fileB.py:10", state: "open", labels: ["observability"] },
  ];
  const plan = JSON.parse(runPlannerSync({ issues }));
  const w1 = waveOf(plan, 1);
  const w2 = waveOf(plan, 2);
  if (!w1 || !w2) throw new Error("Both issues should be assigned to a wave");
  if (w1.wave === w2.wave) {
    throw new Error(
      `Issues 1 and 2 share "observability" label — should be in different waves, both in wave ${w1.wave}`
    );
  }
});

test("label-overlap heuristic allows issues with disjoint labels to share a wave (#369)", () => {
  const issues = [
    { number: 1, title: "A", body: "different/fileA.py:10", state: "open", labels: ["alpha"] },
    { number: 2, title: "B", body: "different/fileB.py:10", state: "open", labels: ["beta"] },
  ];
  const plan = JSON.parse(runPlannerSync({ issues }));
  const w1 = waveOf(plan, 1);
  const w2 = waveOf(plan, 2);
  if (!w1 || !w2) throw new Error("Both issues should be assigned to a wave");
  if (w1.wave !== w2.wave) {
    throw new Error(
      `Issues 1 and 2 have disjoint labels — should be in the same wave, got waves ${w1.wave} and ${w2.wave}`
    );
  }
});

test("MAX_UNIQUE_DEPS_PER_WAVE caps unknown_deps per wave (#369)", () => {
  // With MAX_UNIQUE_DEPS_PER_WAVE=3 and graph coloring's MAX_PER_WAVE=3, a
  // wave can hold at most 3 unknowns. Five unknown_deps issues (no shared
  // file refs, no shared labels) must therefore be split across at least
  // ⌈5/3⌉ = 2 waves, with no wave carrying more than 3 unknowns.
  const issues = [
    { number: 1, title: "U1", body: "no file ref here", state: "open", labels: [] },
    { number: 2, title: "U2", body: "no file ref here", state: "open", labels: [] },
    { number: 3, title: "U3", body: "no file ref here", state: "open", labels: [] },
    { number: 4, title: "U4", body: "no file ref here", state: "open", labels: [] },
    { number: 5, title: "U5", body: "no file ref here", state: "open", labels: [] },
  ];
  const plan = JSON.parse(runPlannerSync({ issues }));
  if (plan.total_issues !== 5) throw new Error(`Expected 5 issues, got ${plan.total_issues}`);

  // Each wave must respect the MAX_UNIQUE_DEPS_PER_WAVE cap.
  for (const wave of plan.waves) {
    const unknowns = wave.issues.filter((i) => !i.has_known_deps).length;
    if (unknowns > 3) {
      throw new Error(
        `Wave ${wave.wave} has ${unknowns} unknowns, exceeds MAX_UNIQUE_DEPS_PER_WAVE=3`
      );
    }
  }
});

test("MAX_UNIQUE_DEPS_PER_WAVE leaves small unknown_deps clusters alone (#369)", () => {
  // 3 unknown_deps issues is at the cap, so they should remain grouped in one wave.
  const issues = [
    { number: 1, title: "U1", body: "no file ref here", state: "open", labels: [] },
    { number: 2, title: "U2", body: "no file ref here", state: "open", labels: [] },
    { number: 3, title: "U3", body: "no file ref here", state: "open", labels: [] },
  ];
  const plan = JSON.parse(runPlannerSync({ issues }));
  const waveNumbers = [1, 2, 3].map((n) => waveOf(plan, n).wave);
  const uniqueWaves = new Set(waveNumbers);
  if (uniqueWaves.size !== 1) {
    throw new Error(
      `3 unknown_deps issues should fit in one wave (cap=3), got waves ${[...uniqueWaves].join(",")}`
    );
  }
});

test("--no-stacking forces single-issue waves for unknown_deps (#369)", () => {
  const issues = [
    { number: 1, title: "U1", body: "no file ref here", state: "open", labels: [] },
    { number: 2, title: "U2", body: "no file ref here", state: "open", labels: [] },
  ];
  const plan = JSON.parse(runPlannerSync({ issues }, ["--no-stacking"]));
  for (const n of [1, 2]) {
    const w = waveOf(plan, n);
    if (!w) throw new Error(`Issue ${n} has no wave assignment`);
    if (w.issues.length !== 1) {
      throw new Error(
        `--no-stacking: issue ${n} should be in a single-issue wave, wave ${w.wave} has ${w.issues.length}`
      );
    }
  }
  if (plan._meta.no_stacking !== true) {
    throw new Error("Expected _meta.no_stacking to be true");
  }
});

test("label-overlap heuristic splits #311 from #307 (issue #369 acceptance)", () => {
  // Real labels from the issue tracker that motivated the heuristic.
  // #307: P2, observability, singleton, error-handling
  // #311: P3, observability, metrics, labels
  // They share "observability" but touch different files, so the old
  // heuristic would co-locate them. The new heuristic must split them.
  const issues = [
    {
      number: 307,
      title: "Singleton guard wrapper silently swallows API errors",
      body: "singleton.py increments HANDLER_TICK_FAILURES_TOTAL before the return None. tests/test_singleton_guard.py wraps a stub handler.",
      state: "open",
      labels: ["P2", "observability", "singleton", "error-handling"],
    },
    {
      number: 311,
      title: "Status-store and tick-failure counters lack namespace/name labels",
      body: "metrics.py + tests/test_metrics_endpoint.py must relabel STATUS_CONFLICTS_TOTAL with (namespace, name).",
      state: "open",
      labels: ["P3", "observability", "metrics", "labels"],
    },
  ];
  const plan = JSON.parse(runPlannerSync({ issues }));
  const w307 = waveOf(plan, 307);
  const w311 = waveOf(plan, 311);
  if (!w307 || !w311) throw new Error("Both #307 and #311 should be assigned waves");
  if (w307.wave === w311.wave) {
    throw new Error(
      `Acceptance criterion: #307 and #311 share "observability" — should be in different waves, both in wave ${w307.wave}`
    );
  }
});

test("MAX_UNIQUE_DEPS_PER_WAVE constant is exposed in _meta (#369)", () => {
  const plan = JSON.parse(runPlannerSync({ issues: [] }));
  if (plan._meta.max_unique_deps_per_wave !== 3) {
    throw new Error(
      `Expected _meta.max_unique_deps_per_wave=3, got ${plan._meta.max_unique_deps_per_wave}`
    );
  }
});

// ============================================================
// Summary
// ============================================================
console.log("\n=== Results ===");
console.log(`  Passed: ${passed}`);
console.log(`  Failed: ${failed}`);
console.log();

if (failed > 0) {
  console.log(`FAILED: ${failed} test(s) failed`);
  process.exit(1);
} else {
  console.log("SUCCESS: All tests passed");
  process.exit(0);
}
