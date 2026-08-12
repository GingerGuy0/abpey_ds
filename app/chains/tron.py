import hashlib
import re
from datetime import datetime, timezone
from app.chains.base import BaseChainClient, IncomingTx, TxNotFound, ChainUnavailable
from app.services.amounts import from_units

def double_sha256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()

def base58_to_hex(b58: str) -> str:
    """
    >>> base58_to_hex("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    '41a614f803b6fd780986a42c78ec9c7f77e6ded13c'
    """
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    num = 0
    for char in b58:
        num = num * 58 + alphabet.index(char)
    b = num.to_bytes(25, byteorder='big')
    return b[:-4].hex()

def hex_to_base58(hex_str: str) -> str:
    """
    >>> hex_to_base58("41a614f803b6fd780986a42c78ec9c7f77e6ded13c")
    'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
    """
    b = bytes.fromhex(hex_str)
    checksum = double_sha256(b)[:4]
    b += checksum
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    num = int.from_bytes(b, byteorder='big')
    res = ''
    while num > 0:
        num, mod = divmod(num, 58)
        res = alphabet[mod] + res
    pad = 0
    for byte in b:
        if byte == 0: pad += 1
        else: break
    return '1' * pad + res

class TronChainClient(BaseChainClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from app.config import settings
        self.api_key = settings.trongrid_api_key

    async def get_incoming_transfers(self, address: str, since: datetime, limit: int = 50) -> list[IncomingTx]:
        url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
        headers = {"TRON-PRO-API-KEY": self.api_key}
        since_ms = int(since.timestamp() * 1000)
        
        params = {
            "contract_address": self.cfg.usdt_contract,
            "only_to": "true",
            "only_confirmed": "true",
            "limit": limit,
            "order_by": "block_timestamp,desc",
            "min_timestamp": since_ms
        }
        
        try:
            data = await self._request("GET", url, params=params, headers=headers)
        except Exception as e:
            raise ChainUnavailable(str(e))
            
        if not data.get("success"):
            raise ChainUnavailable("TronGrid API error")
            
        txs = []
        for tx in data.get("data", []):
            decimals = int(tx.get("token_info", {}).get("decimals", self.cfg.decimals))
            units = int(tx.get("value", 0))
            amount = from_units(units, decimals)
            ts = int(tx.get("block_timestamp", 0)) / 1000.0
            
            txs.append(IncomingTx(
                tx_hash=tx.get("transaction_id", ""),
                network=self.cfg.key,
                from_address=tx.get("from", ""),
                to_address=tx.get("to", ""),
                amount=amount,
                units=units,
                confirmations=self.cfg.min_confirmations, # simplified as only_confirmed=true
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                is_success=True
            ))
            
        return txs

    async def get_tx_by_hash(self, tx_hash: str, address: str) -> IncomingTx:
        url = "https://api.trongrid.io/wallet/gettransactioninfobyid"
        headers = {"TRON-PRO-API-KEY": self.api_key}
        
        try:
            data = await self._request("POST", url, json={"value": tx_hash}, headers=headers)
        except Exception as e:
            raise ChainUnavailable(str(e))
            
        if not data:
            raise TxNotFound("Transaction not found")
            
        receipt = data.get("receipt", {})
        is_success = receipt.get("result") == "SUCCESS"
        
        block_url = "https://api.trongrid.io/wallet/getnowblock"
        block_data = await self._request("POST", block_url, headers=headers)
        current_block = block_data.get("block_header", {}).get("raw_data", {}).get("number", 0)
        
        tx_block = data.get("blockNumber", 0)
        confirmations = max(0, current_block - tx_block) if tx_block > 0 else 0
        
        usdt_hex = base58_to_hex(self.cfg.usdt_contract)[2:] # remove 41
        target_to_hex = base58_to_hex(address)[2:]
        transfer_topic = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        
        for log in data.get("log", []):
            addr = log.get("address", "").lower()
            if addr != usdt_hex.lower():
                continue
            topics = log.get("topics", [])
            if not topics or topics[0].lower() != transfer_topic:
                continue
            if len(topics) >= 3:
                to_addr_log = topics[2][-40:].lower()
                if to_addr_log == target_to_hex.lower():
                    from_addr_log = topics[1][-40:]
                    from_addr = hex_to_base58("41" + from_addr_log)
                    value_hex = log.get("data", "0")
                    units = int(value_hex, 16)
                    amount = from_units(units, self.cfg.decimals)
                    ts = data.get("blockTimeStamp", 0) / 1000.0
                    
                    return IncomingTx(
                        tx_hash=tx_hash,
                        network=self.cfg.key,
                        from_address=from_addr,
                        to_address=address,
                        amount=amount,
                        units=units,
                        confirmations=confirmations,
                        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                        is_success=is_success
                    )
                    
        raise TxNotFound("No matching USDT transfer found")

    def normalize_hash(self, raw: str) -> str:
        raw = raw.lower()
        if raw.startswith("0x"):
            raw = raw[2:]
        if not re.match(r"^[a-f0-9]{64}$", raw):
            raise ValueError("Invalid TRON hash format")
        return raw
