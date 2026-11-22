"""Run the installable external integration example from the source checkout."""

import asyncio

from agentchaos_example_external import (
    StockAnswer,
    create_adapter,
    get_inventory,
    main,
    task_succeeded,
)

__all__ = ["StockAnswer", "create_adapter", "get_inventory", "main", "task_succeeded"]

if __name__ == "__main__":
    asyncio.run(main())
