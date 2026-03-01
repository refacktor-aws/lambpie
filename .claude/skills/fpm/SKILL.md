---
name: fpm
description: Invoke the Functional Project Manager to assess and drive work to completion
---

Invoke the fpm (Functional Project Manager) agent. Pass it the user's request: "$ARGUMENTS"

The fpm will:
1. Dispatch the tpm agent to assess the project and produce a prioritized top-10
2. Present findings with commentary
3. Dispatch the qa-tester agent to validate current state
4. Loop until the request is satisfied or blockers are escalated

If no arguments are provided, use this default request: "Assess the current state of the lambpie project and recommend the next 10 priorities."
