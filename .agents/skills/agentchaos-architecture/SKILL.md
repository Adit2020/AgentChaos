---
name: agentchaos-architecture
description: Ground significant AgentChaos architecture changes in the repository's actual components and boundaries.
---

# AgentChaos architecture

At the time this skill was written, AgentChaos had no application source, package manifest, or Git commits. `AGENTS.md` was empty; `.serena/` contained project configuration only. No runtime components, interfaces, data flows, or architectural boundaries were established.

For significant architectural work, inspect the current repository before designing or changing components. Derive boundaries and invariants from the code and its tests at that time. Do not treat the empty initial checkout as a prescribed architecture or assume that proposed components already exist.
