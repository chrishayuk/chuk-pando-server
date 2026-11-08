# Fly.io deployment: acceptance results (2026-11-07)

**Setup.**
- Deployed from main `8c0a23c`: the gated code (`7c6a0d6`, tested at `54262b9`) plus the Dockerfile path fix. The
  image was later updated (`ca38f30`, then the full-Python fix) only to carry the test-only fault relay.
- Region `lhr`, organisation `personal`.
- `pando-meta`: one shared-cpu-1x 512 MB Machine with a 1 GB volume, **no public IP**, bound to its 6PN address
  only.
- `pando-git`: two shared-cpu-2x 1 GB Machines (A and B; Machine ids omitted from the published copy) behind
  `https://pando-git.fly.dev`, with Basic auth and signed packfile URIs.
- Tigris bucket `pando-packs`.
- Every client operation came from the maintainer's Mac with stock Git. Credentials were held only in a throw-away
  credential store, deleted afterwards.
- Requests were pinned to one frontend with Fly's `fly-force-instance-id` header.

**Host separation.** Not confirmed by Fly. The two frontends' 6PN addresses differ in the host-allocated segment
(`…:4a5:…` against `…:497:…`), which suggests different hosts. Report as **"two Machines"**.

| # | Test | Result |
|---|---|---|
| T1 | Push through A, clone through B; packfile URIs; URI access control through the edge | **Pass.** 8 pushes via A. Through B, a plain clone and a URI clone (3 packs) both had 8/8 commits, fsck-clean. Unsigned, forged-signature and expired URIs → **403**. |
| T2 | Metadata service private | **Pass.** No public IP; bound to its 6PN address; a frontend process with the wrong secret is refused at the handshake. |
| T3 | Read-after-write across Machines | **Pass.** 50/50 rounds: B served the tip A had just acknowledged; 0 mismatches; fsck-clean (66 s). |
| T4 | Same-ref compare-and-swap across Machines | **Pass.** 10/10 rounds of simultaneous pushes via A and B to one branch: exactly one winner, branch at the winner's commit, fsck-clean. |
| T5 | Frontend restart during a push stream | **Pass.** B restarted during 30 pushes via A: 30/30 acknowledged. B (cold cache) then served the last acknowledged tip; 0 acknowledged commits missing; fsck-clean. |
| T6 | Metadata-service outage | **Pass.** A restart was absorbed by the read-retry window (40/40 acknowledged, nothing lost or leaked through either frontend). A 30 s **stop**: the push failed after 10 s, **not acknowledged**, ref unchanged; after restart the same push succeeded at once. Covers then caught up with no further push (L3 1–64 and 65–128, all L2s). |
| T7 | Object-store faults on B only (test relay, TLS end to end, no plaintext seen) | **Pass.** Stall: B's push failed after 11 s, not acknowledged, while **A was unaffected** (acknowledged in 4 s). Refuse: failed after 11 s, not acknowledged. Response trickle: failed after 22 s, not acknowledged. 150 ms latency: acknowledged. Recovery after each: 1 s. |
| T8 | Cross-frontend causal trace | **Pass.** One push via A: ingested → stored (67 ms, including the Tigris upload) → `main` v138 at **LSN 486** → acknowledged (76 ms) → self-contained copy (LSN 488). B, 2 s later: incremental index refresh **LSN 482 → 488 discovered A's pack at LSN 486**, then served the fetch. |

**Video sequence, with the packaged CLI (macOS arm64 release build) from the Mac against the live deployment:**
- `status`; `explain last-push`: version 8, depends on `main@7`, one thin immutable pack, 70 ms of server time,
  copy built;
- `explore covers`;
- `explore fetch --from empty`: 2 covers by URI instead of 8 push packs. At 137 versions: **5 packs by URI instead
  of 137**.

**Problems found during the run, all fixed or recorded:**
- **Dockerfile path.** Fly resolves `[build] dockerfile` relative to the configuration file. Fixed in `8c0a23c`.
- **Image Python.** `python3-minimal` lacks `json` and `http.server`, so the relay could not start. Switched to
  `python3`. While the relay was down, B failed every push safely: nothing acknowledged.
- **Redeploys reset per-Machine settings.** `fly deploy` drops per-Machine environment overrides, so it switched
  B's relay off. Re-apply `PANDO_TEST_RELAY=1` after any deploy if the relay is wanted.
- **macOS Keychain helper.** Apple's Git (Xcode's system configuration) adds the `osxkeychain` credential helper,
  which blocked a scripted run. Test runs use `GIT_CONFIG_NOSYSTEM=1`. Nothing was stored in the Keychain
  (checked).
- **Minor, not fixed.**
  - During a metadata outage the client sees `fatal: Could not read from remote repository`, not a message naming
    the cause. That's correct behaviour, a poor message.
  - The banner prints `http://:::8080` for an IPv6 wildcard.

**State.** The deployment is up, for filming, with the relay off. Fly enforces no spending cap. Usage at this point
is about 40 minutes of the three Machines (≈ $0.03/hour combined), a 1 GB volume, and a few MB in Tigris (free
tier). Verified teardown (`deploy/fly/teardown.sh`) waits for the owner's word that the footage is done.
