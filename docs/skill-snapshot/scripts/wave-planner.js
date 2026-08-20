#!/usr/bin/env node

const fs = require("fs");

const MAX_PER_WAVE = 3;

// Issues #369: structural safety net. If a wave plan groups more than this many
// unknown_deps issues together, force each into its own wave. The file-extraction
// regex cannot predict implicit module coupling, so we treat large clusters of
// unknown_deps as a signal to fall back to single-issue waves.
const MAX_UNIQUE_DEPS_PER_WAVE = 3;

function readInput() {
  if (process.argv.length > 2) {
    return JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  }
  return JSON.parse(fs.readFileSync("/dev/stdin", "utf8"));
}

// Files that are frequently touched by gofmt/struct-alignment even when not
// explicitly mentioned in issue text. These are added to every issue's file list
// to ensure issues that touch them are scheduled in the same wave.
//
// NOTE: These Go-specific placeholder files cause false positives in non-Go projects
// (e.g., Rust). In Rust projects, all issues get assigned the same files, forcing
// them all into a single wave even when they touch completely unrelated code.
// Use --rust-mode (or auto-detect via absence of Go files) to skip these.
const HIGH_COLLISION_FILES = [
  "cmd/nexus/main.go",
  "cmd/nexus/main_test.go",
];

// Mode flags
let RUST_MODE = false;

function extractFileRefs(text) {
  if (!text) return [];
  const files = new Set();

  // Skip high-collision Go placeholder files in Rust mode — they cause all issues
  // to share the same file list, collapsing multiple waves into one.
  if (!RUST_MODE) {
    for (const f of HIGH_COLLISION_FILES) {
      files.add(f);
    }
  }

  // Match file paths with optional line numbers, supported extensions,
  // and common delimiters (backticks, quotes, brackets, or whitespace).
  // Handles: `cmd/nexus/main.go:42`, "internal/auth/auth.go:10", [pkg/foo/bar.ts:5]
  const pathPatterns = [
    // Backtick, quote, or bracket delimited: `dir/subdir/file.ext:123`
    /[`'"\[\s]([a-zA-Z0-9_./-]+\/[a-zA-Z0-9_./-]+\.[a-z]{2,4})(?::\d+)?[`'"\]\s]/g,
    // Colon-separated with line number: internal/auth/auth.go:42  (no surrounding chars needed)
    /(?<![a-zA-Z0-9_/.-])([a-zA-Z0-9_./-]+\/[a-zA-Z0-9_./-]+\.[a-z]{2,4}):(\d+)/g,
    // Bare quoted or backtick path: "cmd/nexus/main.go" or just cmd/nexus/main.go as last resort
    /[`'"]([a-zA-Z0-9_./-]+\.[a-z]{2,4})[`'"]/g,
    // Extension-only files that are clearly file paths (require path separator or common prefix)
    /\b([a-zA-Z0-9_./-]+\/(?:src|lib|test|tests|pkg|cmd|internal|osimflow|bin|docs|scripts|app|modules|components)\/[a-zA-Z0-9_./-]+\.[a-z]{2,4})\b/g,
  ];

  for (const pat of pathPatterns) {
    let m;
    while ((m = pat.exec(text)) !== null) {
      const f = m[1];
      if (!f.includes("http") && !f.includes("://") && f.length > 3) {
        files.add(f);
      }
    }
  }

  return [...files];
}

function extractModuleRefs(text) {
  if (!text) return [];
  const modules = new Set();

  const patterns = [
    /import\s+.+\s+from\s+['"](\.?\.?\/[^'"]+)['"]/g,
    /from\s+([a-zA-Z0-9_.]+)\s+import/g,
    /require\(['"](\.?\.?\/[^'"]+)['"]\)/g,
    /use\s+([a-zA-Z0-9_:]+::[a-zA-Z0-9_:]+)/g,
  ];

  for (const pat of patterns) {
    let m;
    while ((m = pat.exec(text)) !== null) {
      modules.add(m[1]);
    }
  }

  return [...modules];
}

function analyzeIssue(issue) {
  const body = issue.body || "";
  const title = issue.title || "";
  const fullText = `${title}\n${body}`;

  const fileRefs = extractFileRefs(fullText);
  const moduleRefs = extractModuleRefs(fullText);

  const affectedFiles = [...new Set([...fileRefs, ...moduleRefs])];
  const hasKnownDeps = affectedFiles.length > 0;

  return {
    number: issue.number,
    title: title,
    labels: (issue.labels || []).map((l) =>
      typeof l === "string" ? l : l.name || ""
    ),
    affected_files: affectedFiles,
    has_known_deps: hasKnownDeps,
  };
}

function buildConflictGraph(analyzed) {
  const n = analyzed.length;
  const adj = Array.from({ length: n }, () => new Set());

  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const a = analyzed[i];
      const b = analyzed[j];

      const sharesFiles = a.affected_files.some((f) =>
        b.affected_files.includes(f)
      );
      // Issue #369: two issues that share >=1 label are flagged as additional
      // conflicts. The label-overlap heuristic replaces the old "both unknown
      // always conflicts" rule — unknown_deps issues without shared labels can
      // now cluster (capped by MAX_UNIQUE_DEPS_PER_WAVE in the post-coloring
      // pass), while label overlap (e.g. #307 and #311 sharing "observability")
      // still forces a split.
      const sharedLabel = a.labels.some((l) => b.labels.includes(l));

      if (sharesFiles || sharedLabel) {
        adj[i].add(j);
        adj[j].add(i);
      }
    }
  }

  return adj;
}

function graphColoring(adj, n, maxPerColor) {
  const colors = new Array(n).fill(-1);
  const colorCounts = [];

  for (let node = 0; node < n; node++) {
    const usedColors = new Set();
    for (const neighbor of adj[node]) {
      if (colors[neighbor] !== -1) {
        usedColors.add(colors[neighbor]);
      }
    }

    let assigned = -1;
    for (let c = 0; c < colorCounts.length; c++) {
      if (!usedColors.has(c) && colorCounts[c] < maxPerColor) {
        assigned = c;
        break;
      }
    }

    if (assigned === -1) {
      assigned = colorCounts.length;
      colorCounts.push(0);
    }

    colors[node] = assigned;
    colorCounts[assigned]++;
  }

  return colors;
}

function planWaves(issues, opts = {}) {
  if (!issues || issues.length === 0) {
    return { waves: [], total_issues: 0, total_waves: 0 };
  }

  const analyzed = issues.map(analyzeIssue);
  const adj = buildConflictGraph(analyzed);
  const colors = graphColoring(adj, analyzed.length, MAX_PER_WAVE);

  const maxWave = Math.max(...colors) + 1;
  const rawWaves = [];
  for (let w = 0; w < maxWave; w++) {
    const waveIssues = analyzed.filter((_, i) => colors[i] === w);
    if (waveIssues.length > 0) {
      rawWaves.push(waveIssues);
    }
  }

  // Issue #369: unknown_deps safety net. After the graph-coloring assignment,
  // demote clustered unknown_deps issues to single-issue waves if either:
  //   (a) --no-stacking was passed (orthogonal opt-in flag), or
  //   (b) the wave has more than MAX_UNIQUE_DEPS_PER_WAVE unknown_deps issues
  //       (structural safety net, active by default).
  const noStacking = !!opts.noStacking;
  const cap = noStacking ? 0 : MAX_UNIQUE_DEPS_PER_WAVE;
  const finalWaves = [];
  for (const wave of rawWaves) {
    const unknowns = wave.filter((i) => !i.has_known_deps);
    const knowns = wave.filter((i) => i.has_known_deps);
    if (unknowns.length > cap) {
      for (const u of unknowns) {
        finalWaves.push([u]);
      }
      if (knowns.length > 0) {
        finalWaves.push(knowns);
      }
    } else {
      finalWaves.push(wave);
    }
  }

  const waves = finalWaves.map((issues, idx) => ({
    wave: idx + 1,
    issues,
  }));

  return {
    total_issues: analyzed.length,
    total_waves: waves.length,
    waves,
  };
}

const USAGE = `wave-planner.js — group GitHub issues into parallelization waves

Usage:
  gh issue list --json number,title,body,labels,state | node wave-planner.js [FILE]
  node wave-planner.js issues.json

Options:
  --dry-run, -n   Show affected_files analysis without generating wave plans.
                  Use this to review file-reference extraction before execution.
  --rust-mode, -r Skip Go-specific high-collision placeholder files
                  (cmd/nexus/main.go, cmd/nexus/main_test.go) that cause
                  false file-share conflicts in non-Go projects.
                  Auto-detected if no Go files appear in issue text.
  --no-stacking   Force single-issue waves for any unknown_deps issue
                  (issue #369). Orthogonal to the label-overlap heuristic
                  and the MAX_UNIQUE_DEPS_PER_WAVE cap, which remain active
                  by default.
  --help, -h      Show this help message.

Reads JSON from stdin or a file (accepts raw array or {issues: [...]} wrapper).
Filters out already-closed issues, groups remaining issues by file-conflict graph,
and outputs up to MAX_PER_WAVE (=3) issues per wave.

Examples:
  # Plan waves for all open issues (auto-detects Rust/Go based on content)
  gh issue list --state open --json number,title,body,labels | node wave-planner.js

  # Explicitly set Rust mode for non-Go projects
  gh issue list --state open --json number,title,body,labels | node wave-planner.js --rust-mode

  # Review affected_files before planning
  gh issue list --state open --json number,title,body,labels | node wave-planner.js --dry-run

  # Plan waves from a file
  node wave-planner.js /tmp/my-issues.json
`;

// Detect Rust mode by checking if issue text contains Go-specific file paths.
// If no Go placeholder files are found, assume Rust/non-Go project.
function detectRustMode(text) {
  if (!text) return false;
  return !HIGH_COLLISION_FILES.some((f) => text.includes(f));
}

function main() {
  if (process.argv.includes("--help") || process.argv.includes("-h")) {
    process.stdout.write(USAGE);
    return;
  }

  const isDryRun = process.argv.includes("--dry-run") || process.argv.includes("-n");
  RUST_MODE = process.argv.includes("--rust-mode") || process.argv.includes("-r");
  const noStacking = process.argv.includes("--no-stacking");

  const input = readInput();
  let issues = Array.isArray(input) ? input : input.issues || [];

  // Auto-detect Rust mode: if no Go placeholder files appear in any issue text,
  // treat this as a non-Go project and skip high-collision file injection.
  if (!RUST_MODE) {
    const allText = issues.map((iss) => `${iss.title || ""} ${iss.body || ""}`).join(" ");
    RUST_MODE = detectRustMode(allText);
  }

  // Filter out already-closed issues
  const before = issues.length;
  issues = issues.filter((iss) => {
    const state = typeof iss.state === "string" ? iss.state : "open";
    return state.toLowerCase() !== "closed";
  });
  const filtered = before - issues.length;

  if (isDryRun) {
    // --dry-run: output affected_files analysis without generating wave plans
    const analyzed = issues.map(analyzeIssue);
    const dryRunResult = {
      _meta: {
        filtered_closed: filtered,
        mode: "dry-run",
        rust_mode: RUST_MODE,
        total_issues: analyzed.length,
      },
      issues: analyzed.map((a) => ({
        number: a.number,
        title: a.title,
        affected_files: a.affected_files,
        has_known_deps: a.has_known_deps,
      })),
    };
    console.log(JSON.stringify(dryRunResult, null, 2));
    return;
  }

  const plan = planWaves(issues, { noStacking });
  plan._meta = {
    filtered_closed: filtered,
    rust_mode: RUST_MODE,
    no_stacking: noStacking,
    max_unique_deps_per_wave: MAX_UNIQUE_DEPS_PER_WAVE,
  };
  console.log(JSON.stringify(plan, null, 2));
}

main();
