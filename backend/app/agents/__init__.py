"""Agents package.

Agents are added one by one, milestone by milestone:
- M1: router (request type detection) + mock pipeline agents
- M3: requirements, architecture, datasheet
- M4: cubemx, firmware
- M5: review, debug, optimization, test
- M6: docs

Each agent resolves its model at runtime via get_agent_llm(<name>):
DB override (agent_settings) -> default LLM_MODEL from .env.
"""

# Canonical agent names — used by the /agents/settings API and (later) the
# dashboard Agents page (M7).
KNOWN_AGENTS: list[str] = [
    "router",
    "requirements",
    "architecture",
    "datasheet",
    "cubemx",
    "firmware",
    "review",
    "debug",
    "optimization",
    "test",
    "docs",
]
