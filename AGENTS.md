# Working in this repository (humans and AI agents)

Several agents work on this toolchain at the same time. Git is the coordination
layer: **one task = one branch = one worktree**. Branches record claimed work,
`main` is always green, and history replaces the old copy-the-tree snapshots.

## Layout

| Path | What |
|---|---|
| `assembler/` | TS201 assembler (`ts201` package) |
| `compiler/` | C compiler (`ts201cc`), IR optimizer (`-O1/-O2`), vendored pycparser |
| `linker/` | `.tso` object linker (`ts201ld`) |
| `emulator/` | Minimal TS201 integer emulator and console Hello World |
| `tools/check.py` | The merge gate (see below) |
| `TASKS.md` | Open work backlog |

Outside the repo: do not modify or commit external projects or proprietary assets.

## Environment

- Python 3.10+. Set `TS201_PYTHON` to select an interpreter when `python`
  is not on PATH; the `.ps1` launchers also look for the bundled runtime.
- Build and verification use Python only. Full checks emit corpus images;
  emulator tests execute small programs within the supported instruction subset.
- Keep large build output on a volume with sufficient free space.

## Workflow

### 1. Claim a task by creating a branch in its own worktree

```sh
# From the main checkout:
git worktree add "../ts201-worktrees/<topic>" -b agent/<topic> main
```

- Branch names: `agent/<topic>` (short, kebab-case, describes the work, e.g.
  `agent/simd-xy-pairing`). One branch per task; don't reuse branches.
- **Check first:** `git branch --list "agent/*"` and `git worktree list` show
  what everyone is doing. If a branch already covers your task, don't start a
  parallel one - pick something else or coordinate through the user.
- Never edit files in the main checkout (`ts201-toolchain/`). It stays on
  `main` and is only touched by fast-forward merges. Claude Code's built-in
  worktree isolation (`.claude/worktrees/`) is also fine.

### 2. Work in small commits

- Subject: `<component>: <imperative summary>` where component is
  `assembler`, `compiler`, `linker`, `emulator`, `tools` or `repo`.
  Body: why, plus the evidence (tests run, packet counts).
- Commit often. Before you stop for any reason, commit what you have (prefix
  `wip:` if it's not passing) with a body saying what's done, what's left, and
  what you were in the middle of - the next agent picks up from that commit.
- Stay in your lane: don't reformat or refactor code outside your task; that is
  what causes merge conflicts between agents. `compiler/ts201cc/opt.py`,
  `loops.py`, `isel.py` and `backend.py` are hot spots - keep diffs there tight.

### 3. Rebase, verify, merge

```sh
git rebase main                                              # from your worktree (branches are shared by all worktrees)
python tools/check.py                                        # always
python tools/check.py --full                                 # if you touched codegen, encodings or linker output
# From the main checkout after verification:
git merge --ff-only agent/<topic>
git worktree remove "../ts201-worktrees/<topic>"
git branch -d agent/<topic>
```

- `main` only moves by **fast-forward**. If `--ff-only` fails, someone merged
  first: rebase again, re-run checks, retry. Never merge a red branch.
- When you finish a `TASKS.md` item, delete it in the same branch.

Compiler debug knobs: `TS201_DISABLE=<pass,...>` (sched, prera, gvn, licm,
inline, unroll, version, reassoc, licm2, ysplit, mpipe) and
`TS201_NOOPT_FUNCS=f1,f2`. `-O0` is the original generator and the reference
for differential testing.

## Never

- Commit proprietary assets, generated `build/` output or files > 1 MiB.
- Rewrite `main` (`push --force`, `reset` on main, rebasing merged commits).
- Run `git reset --hard`, `git clean`, `git stash` or `git checkout -- .` in a
  worktree that isn't yours - it destroys another agent's uncommitted work.
- Turn a known correctness failure into a silent skip or describe static tests
  as proof of target execution (see `assembler/CONTRIBUTING.md`).
- Import vendor (ADI) executables, libraries, or runtime code.
