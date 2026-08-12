from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re
import aiohttp
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from app.config import NetworkConfig

@dataclass(frozen=True)
class IncomingTx:
    tx_hash: str
    network: str
    from_address: str
    to_address: str
    amount: Decimal
    units: int
    confirmations: int
    timestamp: datetime
    is_success: bool

class ChainError(Exception):
    pass

class TxNotFound(ChainError):
    pass

class ChainRateLimited(ChainError):
    pass

class ChainUnavailable(ChainError):
    pass

class ChainAuthError(ChainError):
    pass

class BaseChainClient(ABC):
    def __init__(self, cfg: NetworkConfig, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session

    @abstractmethod
    async def get_incoming_transfers(self, address: str, since: datetime, limit: int = 50) -> list[IncomingTx]:
        pass

    @abstractmethod
    async def get_tx_by_hash(self, tx_hash: str, address: str) -> IncomingTx:
        pass

    @abstractmethod
    def normalize_hash(self, raw: str) -> str:
        pass

    def validate_address(self, address: str) -> bool:
        return bool(re.match(self.cfg.address_regex, address))

    @retry(
        retry=retry_if_exception_type(ChainRateLimited),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True
    )
    async def _request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=15))
        async with self.session.request(method, url, **kwargs) as response:
            if response.status == 429:
                raise ChainRateLimited("Rate limited")
            try:
                response.raise_for_status()
                return await response.json()
            except aiohttp.ContentTypeError as e:
                import logging
                text = await response.text()
                logging.error(f"Non-JSON response from {url}: {response.status} {text[:300]}")
                raise ChainUnavailable(f"Non-JSON response: {response.status}") from e
            except aiohttp.ClientResponseError as e:
                import logging
                logging.error(f"HTTP Error {e.status} from {url}")
                raise ChainUnavailable(f"HTTP Error {e.status}") from e
