import time
import aiohttp
from app.config import NETWORKS, settings
from app.chains.base import BaseChainClient
from app.chains.evm import EvmChainClient
from app.chains.tron import TronChainClient
from app.chains.solana import SolanaChainClient

class NetworkDisabled(Exception):
    pass

class ChainRegistry:
    def __init__(self):
        self.session = None
        self._clients: dict[str, BaseChainClient] = {}
        self._available_cache = None
        self._available_cache_ts = 0

    def start(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
            )

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None

    def supported(self) -> list[str]:
        keys = []
        for k in settings.enabled_network_keys:
            if settings.network_has_credentials(k):
                keys.append(k)
        return keys

    def get(self, network_key: str) -> BaseChainClient:
        if network_key not in self.supported():
            raise NetworkDisabled(f"Network {network_key} is disabled or missing credentials")
            
        if network_key in self._clients:
            return self._clients[network_key]
            
        if self.session is None:
            raise RuntimeError("ChainRegistry not started")
            
        cfg = NETWORKS.get(network_key)
        if not cfg:
            raise ValueError(f"Unknown network: {network_key}")
            
        if network_key in ("bsc", "eth"):
            urls = settings.rpc_urls(network_key)
            if urls:
                from app.chains.evm_rpc import EvmRpcChainClient
                client = EvmRpcChainClient(cfg, self.session, urls)
            else:
                client = EvmChainClient(cfg, self.session)
        elif network_key == "tron":
            client = TronChainClient(cfg, self.session)
        elif network_key == "sol":
            client = SolanaChainClient(cfg, self.session)
        else:
            raise ValueError(f"No client implementation for network: {network_key}")
            
        self._clients[network_key] = client
        return client

    async def available(self, session) -> list[str]:
        now = time.time()
        if self._available_cache is not None and now - self._available_cache_ts < 60:
            return self._available_cache
            
        from app.services import repo
        
        valid_keys = []
        for k in self.supported():
            wallet = await repo.get_wallet(session, k)
            if wallet:
                valid_keys.append(k)
                
        self._available_cache = valid_keys
        self._available_cache_ts = now
        return valid_keys
        
    def invalidate_available_cache(self):
        self._available_cache = None
        self._available_cache_ts = 0

registry = ChainRegistry()
