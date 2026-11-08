# Held-out suites (Teams C and D): NEVER mounted into builder environments
Contents to be authored independently of builders and of OpenAI's fork tests (fork tests may inform fixtures only; no copied cases):
- conformance cases for CG1 (feature matrix), CG3 (stock-pack dependency cases 1–7 in spec/03), CG2 crash schedules, CG4 litmus tests
- adversarial/concurrency/failure-injection schedules
- benchmark workloads W (10/100/1,000 agents) and negative workloads
**Launch precondition:** these exist, are committed here, and are hashed in `MANIFEST.sha256` BEFORE any builder starts. They are absent today.
