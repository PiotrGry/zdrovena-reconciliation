"""Azure Table Storage backend for close history."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("zdrovena.month_closing.table_history")

TABLE_NAME = "closehistory"
PARTITION_KEY = "close"


def append_history_table(storage_conn_or_url: str, entry: dict) -> None:
    """Append one entry to Azure Table Storage."""
    try:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        if storage_conn_or_url.startswith("http"):
            client = TableServiceClient(
                endpoint=storage_conn_or_url,
                credential=DefaultAzureCredential(),
            )
        else:
            client = TableServiceClient.from_connection_string(storage_conn_or_url)

        table = client.create_table_if_not_exists(TABLE_NAME)

        ts = entry.get("ts", "")
        ts_safe = ts.replace(":", "-").replace(".", "-").replace("+", "-")
        row_key = f"{entry.get('year', 0):04d}-{entry.get('month', 0):02d}-{ts_safe}"

        entity = {
            "PartitionKey": PARTITION_KEY,
            "RowKey": row_key,
            **{
                k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                for k, v in entry.items()
                if k not in ("PartitionKey", "RowKey")
            },
        }
        table.upsert_entity(entity)
    except Exception as exc:
        logger.warning("Could not append close history to Table Storage: %s", exc)


def read_history_table(storage_conn_or_url: str, limit: int = 50) -> list[dict]:
    """Read last N entries, newest first."""
    try:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        if storage_conn_or_url.startswith("http"):
            client = TableServiceClient(
                endpoint=storage_conn_or_url,
                credential=DefaultAzureCredential(),
            )
        else:
            client = TableServiceClient.from_connection_string(storage_conn_or_url)

        table = client.get_table_client(TABLE_NAME)

        entities = list(
            table.query_entities(
                f"PartitionKey eq '{PARTITION_KEY}'",
                select=None,
            )
        )

        # Sort by RowKey descending (year-month-ts format sorts chronologically)
        entities.sort(key=lambda e: e.get("RowKey", ""), reverse=True)
        entities = entities[:limit]

        result = []
        for entity in entities:
            row: dict = {}
            for k, v in entity.items():
                if k in ("PartitionKey", "RowKey", "etag", "Timestamp"):
                    continue
                # Deserialize JSON-encoded list/dict fields
                if isinstance(v, str):
                    try:
                        parsed = json.loads(v)
                        if isinstance(parsed, (list, dict)):
                            v = parsed
                    except (json.JSONDecodeError, ValueError):
                        pass
                row[k] = v
            result.append(row)
        return result
    except Exception as exc:
        logger.warning("Could not read close history from Table Storage: %s", exc)
        return []


def delete_history_entry_table(storage_conn_or_url: str, ts: str) -> bool:
    """Delete entry by ts. Returns True if found and deleted."""
    try:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        if storage_conn_or_url.startswith("http"):
            client = TableServiceClient(
                endpoint=storage_conn_or_url,
                credential=DefaultAzureCredential(),
            )
        else:
            client = TableServiceClient.from_connection_string(storage_conn_or_url)

        table = client.get_table_client(TABLE_NAME)

        ts_safe = ts.replace(":", "-").replace(".", "-").replace("+", "-")

        # Query for entities matching this ts — RowKey contains ts_safe
        entities = list(
            table.query_entities(
                f"PartitionKey eq '{PARTITION_KEY}'",
                select=["PartitionKey", "RowKey", "ts"],
            )
        )

        deleted = False
        for entity in entities:
            if entity.get("ts") == ts:
                table.delete_entity(
                    partition_key=entity["PartitionKey"],
                    row_key=entity["RowKey"],
                )
                deleted = True

        return deleted
    except Exception as exc:
        logger.warning(
            "Could not delete close history entry %s from Table Storage: %s", ts, exc
        )
        return False
