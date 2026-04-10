from __future__ import annotations

import re
from datetime import UTC, datetime

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from agent_integrations.storage import SQLiteRunStore


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    combined = f'mcp__{server_name}__{tool_name}'
    return re.sub(r'[^a-zA-Z0-9_-]', '_', combined)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


class OAuthTokenStore:
    def __init__(self, store: SQLiteRunStore, server_name: str) -> None:
        self.store = store
        self.server_name = server_name

    async def get_tokens(self) -> OAuthToken | None:
        payload = self.store.load_oauth_tokens(self.server_name)
        if payload is None:
            return None
        return OAuthToken.model_validate(payload)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.store.save_oauth_tokens(self.server_name, tokens.model_dump(mode='json', exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        payload = self.store.load_oauth_client_info(self.server_name)
        if payload is None:
            return None
        return OAuthClientInformationFull.model_validate(payload)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_oauth_client_info(self.server_name, client_info.model_dump(mode='json', exclude_none=True))
