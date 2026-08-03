# Update-path E2E testing — plan

**Status:** v1 in progress (routes 1 & 2). Linux only.
**Goal:** prove in CI that a user on `main` can get to this PR's commit, by
every update route we support.

## Why

We have ~17k unit tests and zero coverage of the thing that breaks worst: the
update. A broken updater is uniquely bad — it strands users on the version that
cannot fix itself. `hermes update` alone is ~2000 lines
(`hermes_cli/update_cmd.py`), and nothing exercises it end to end.

The shape of the test is always the same:

1. Build a fake `main` = real upstream `main` + this PR's commit merged in.
2. Install *real upstream `main`* the way a user would.
3. Update via the route under test.
4. Assert HEAD landed on the merge commit and the install still works
   (`hermes --version`, `hermes doctor`).

`scripts/dev-sandbox.sh` already does 1 and 2 (`install --from-main` fetches
genuine upstream main, parks this folder at `refs/hermes-sandbox/next`, and
promotes it to fake `main` after install — exactly the "newer main is waiting"
state an update needs).

## The routes

Grounded in code, not guessed. Linux only; Windows/macOS deferred.

| # | Route | Entry point | v1? |
|---|-------|-------------|-----|
| 1 | Re-run `install.sh` over an existing checkout | `scripts/install.sh` (autostash → pull → deps) | **yes** |
| 2 | `hermes update` | `update_cmd.py::_cmd_update_impl` | **yes** |
| 3 | Desktop app "Update" button | `apps/desktop/electron/main.ts::applyUpdatesPosixInApp` → `hermes update --yes --branch <healed>` then `hermes desktop --build-only` | no |
| 4 | `/update` from a messaging platform | `gateway/run.py` → `_handle_update_command` (gateway-mode file-IPC prompts) | no |
| 5 | Manual `git pull` + `uv pip install -e ".[all]"` on a self-managed checkout | docs `updating.md` "Manual Update" | no |
| 6 | Docker — `docker pull` | `config.py::_DOCKER_UPDATE_MESSAGE`; `hermes update` **refuses** | no |
| 7 | Nix — `nix profile upgrade` / flake rebuild | `config.py::_NIX_UPDATE_MSG`; `hermes update` **refuses** | no |

Routes 6 and 7 are refusal paths: the useful assertion is that `hermes update`
*declines with the right guidance*, which is cheap and unit-testable — it hangs
off `detect_install_method()` (`hermes_cli/config.py:410`).

### Install-layout axis

Orthogonal to the route, and easy to miss: `install.sh` picks its layout from
`id -u` alone (`resolve_install_layout`, `install.sh:422`).

| uid | Code | Command | Data |
|-----|------|---------|------|
| non-root | `$HERMES_HOME/hermes-agent` | `~/.local/bin/hermes` | `~/.hermes` |
| root (Linux) | `/usr/local/lib/hermes-agent` | `/usr/local/bin/hermes` | `/root/.hermes` |

Both need coverage. `dev-sandbox.sh` now defaults to the user-level layout (what
most people run) with `--root` for FHS. Note the root path also redirects
`UV_PYTHON_INSTALL_DIR` to `/usr/local/share/uv` for world-readability
(#21457), so the two layouts differ in more than paths.

### How the user-level sandbox gets a network

Worth recording, because the failure is non-obvious. slirp4netns joins the
target's userns and setuids to root before configuring the netns, so the userns
**must map a uid 0**. bwrap's `--unshare-user` maps exactly one uid, so
`--uid 1000` left no root for slirp to become and it died with
`setns(CLONE_NEWNET): Operation not permitted`.

The script now creates the namespaces itself in a stage-1 launcher, with two
one-id ranges:

```
inner 0     <- a subuid   (never used by the payload; exists so slirp can be root)
inner 1000  <- our real host uid
```

Mapping inner 1000 to the *host* uid (rather than another subuid) is what keeps
everything the sandbox writes owned by us, so `rm -rf .hermes-sandbox` still
works with no chown dance. Stage 2 then runs bwrap **without** `--unshare-user`
— it only adds the mount/pid namespaces — which sidesteps bwrap's refusal to
accept `--uid` outside a userns it created. `unshare --user` grants its creator
full capabilities in the new userns regardless of mapped uid, so bwrap can still
mount as uid 1000.

Cost: a `/etc/subuid` + `/etc/subgid` range for the invoking user (the script
errors with the exact line to add if missing) and util-linux `unshare`. `--root`
needs neither. **CI implication:** GitHub runners need to be checked for subuid
allocations before Tier B can use the default mode; if they lack them, Tier B
runs `--root` and Tier A covers the user-level layout.

## Two tiers of test

**Tier A — git-level, no container.** Point the installer's two hardcoded repo
URLs at a local bare repo via a *multi-valued* `url.<file://…>.insteadOf`
rewrite, then run the real update code. Verified working: a `--depth 1` clone
followed by `hermes update`'s scoped `git fetch origin main` fast-forwards
correctly, with `GIT_SSH_COMMAND=false` proving nothing touches the network.
Fast, unprivileged, runs on any runner. This is where the "does HEAD move"
contract belongs.

**Tier B — full dev-sandbox.** Real `curl … | bash` through the sandbox's MITM
proxy, real ssh shim, real FHS install, no host writes. Highest fidelity.
Needs `bubblewrap` + `slirp4netns` on the runner and probably
`kernel.apparmor_restrict_unprivileged_userns=0` on ubuntu-24.04.

Order: Tier A first (mergeable, no CI privilege questions), Tier B once bwrap
is proven to survive on a GitHub runner.

## Known traps

- **Pipe-hides-failure.** `sandbox install … | tail` reports exit 0 even when
  the installer dies; the symptom is a confusing `curl: (23) Failure writing
  output to destination`. Any CI script needs `set -o pipefail`.
- **Sandbox flag ordering.** `dev-sandbox.sh` stops parsing at the first
  argument it doesn't recognize and passes the rest through, so sandbox flags
  must precede `--`. `install --skip-setup --from-main` sends `--from-main` to
  the installer, which rejects it.
- **`insteadOf` is multi-valued.** A second plain `git config` on the same key
  *replaces* rather than appends, silently letting one URL escape to real
  GitHub. Use `--add` and assert both rewrites with `--get-all`.
- **Shallow clones.** The installer clones `--depth 1`; the updater fetches a
  single scoped ref. Any fixture must reproduce both or it tests nothing real.
- **A dirty worktree changes fake main.** dev-sandbox snapshots uncommitted
  changes into a temp commit, so CI must either commit first or expect the
  snapshot SHA, not `HEAD`.
- **`--commit` never rolls backwards** without `--force-commit`
  (`install.sh:1382`) — an installer-driven test that pins an older commit
  will silently no-op.

## Open items

- **Tier B on a GitHub runner.** Needs `bubblewrap` + `slirp4netns` +
  `util-linux`, and likely
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` on
  ubuntu-24.04. Also needs a `/etc/subuid` range for the runner user if it uses
  the default user-level mode. Unverified — check before committing to Tier B.
- **Routes 3–7.** Deferred (see table). Route 3 is the highest-value next one,
  since the desktop button is how most non-terminal users update; it shells the
  same `hermes update`, so route 2's coverage carries most of the risk.
- **macOS / Windows.** Out of scope for now. The Windows installer path had
  prior AutoHotkey automation on a branch (`ef7749d4c`, never merged to main);
  worth revisiting once Linux is green.
