import asyncio
import json

from app.ingestion.service import get_fda_ingestion_coordinator


async def main() -> None:
    coordinator = get_fda_ingestion_coordinator()
    result = await coordinator.backfill_embeddings()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
