"""Contracts for integrations that translate agent executions into domain events."""

from agentchaos.adapters.base import AgentAdapter, FaultCapableAdapter

__all__ = ["AgentAdapter", "FaultCapableAdapter"]
