import logging
from dataclasses import dataclass
from decimal import Decimal
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class NetworkConfig:
    key: str
    title: str
    emoji: str
    usdt_contract: str
    decimals: int
    min_confirmations: int
    explorer_tx_url_template: str
    address_regex: str
    chainid: int | None = None
    # Середній час блоку в секундах. Потрібен, щоб порахувати, скільки блоків
    # назад дивитися при першому скануванні (замість магічних констант).
    block_time_sec: float = 12.0


NETWORKS: dict[str, NetworkConfig] = {
    "bsc": NetworkConfig(
        key="bsc",
        title="BNB Smart Chain",
        emoji="🟡",
        usdt_contract="0x55d398326f99059fF775485246999027B3197955",
        decimals=18,
        min_confirmations=12,
        explorer_tx_url_template="bscscan.com/tx/{}",
        address_regex=r"^0x[a-fA-F0-9]{40}$",
        chainid=56,
        block_time_sec=0.75
    ),
    "eth": NetworkConfig(
        key="eth",
        title="Ethereum",
        emoji="🔵",
        usdt_contract="0xdAC17F958D2ee523a2206206994597C13D831ec7",
        decimals=6,
        min_confirmations=12,
        explorer_tx_url_template="etherscan.io/tx/{}",
        address_regex=r"^0x[a-fA-F0-9]{40}$",
        chainid=1,
        block_time_sec=12.0
    ),
    "tron": NetworkConfig(
        key="tron",
        title="TRON",
        emoji="🔴",
        usdt_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        decimals=6,
        min_confirmations=20,
        explorer_tx_url_template="tronscan.org/#/transaction/{}",
        address_regex=r"^T[1-9A-HJ-NP-Za-km-z]{33}$",
    ),
    "sol": NetworkConfig(
        key="sol",
        title="Solana",
        emoji="🟣",
        usdt_contract="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        decimals=6,
        min_confirmations=1,
        explorer_tx_url_template="solscan.io/tx/{}",
        address_regex=r"^[1-9A-HJ-NP-Za-km-z]{32,44}$",
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    owner_id: int = 0
    channel_id: int = 0
    # Режим тільки long polling. Webhook не реалізований: на Koyeb бот живе як
    # Worker Service, у якого взагалі немає HTTP-порту.
    database_url: str = "sqlite+aiosqlite:///./bot.db"
    log_level: str = "INFO"
    
    etherscan_api_key: str = ""
    trongrid_api_key: str = ""
    tonapi_key: str = ""
    solana_rpc_url: str = ""
    # Порядок має значення: спершу ноди, які обслуговують eth_getLogs (потрібен
    # для автопошуку платежів), потім bsc-dataseed* — вони швидкі для receipt,
    # але на eth_getLogs відповідають "limit exceeded" за будь-якого діапазону.
    bsc_rpc_urls: str = "https://bsc-rpc.publicnode.com,https://1rpc.io/bnb,https://binance.llamarpc.com,https://bsc-dataseed.bnbchain.org,https://bsc-dataseed1.defibit.io"
    eth_rpc_urls: str = ""
    enabled_networks: str = ""
    # Стартовий розмір вікна для eth_getLogs. Публічні ноди мають різні (і
    # недокументовані) ліміти, тому клієнт зменшує це значення автоматично,
    # щойно нода відповість "limit exceeded".
    rpc_logs_chunk_blocks: int = 500
    
    # Додаткові адміни (tg_id через кому). owner_id завжди адмін і без цього
    # списку, тому дефолт порожній — жодних чужих ID у публічному репозиторії.
    admin_ids: str = ""

    wallet_bsc: str = ""
    wallet_eth: str = ""
    wallet_tron: str = ""
    wallet_ton: str = ""
    wallet_sol: str = ""

    @property
    def enabled_network_keys(self) -> list[str]:
        if not self.enabled_networks.strip():
            return list(NETWORKS.keys())
            
        keys = [k.strip().lower() for k in self.enabled_networks.split(",")]
        keys = [k for k in keys if k]
        
        valid_keys = []
        for k in keys:
            if k in NETWORKS:
                valid_keys.append(k)
            else:
                logging.warning(f"Unknown network in enabled_networks: {k}")
                
        if not valid_keys:
            return list(NETWORKS.keys())
            
        return valid_keys

    @property
    def admin_id_list(self) -> list[int]:
        """Усі адміни: власник + ADMIN_IDS з .env."""
        ids: list[int] = []
        if self.owner_id:
            ids.append(self.owner_id)

        for raw in self.admin_ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                tg_id = int(raw)
            except ValueError:
                logging.warning(f"Некоректний tg_id в ADMIN_IDS: {raw!r}")
                continue
            if tg_id not in ids:
                ids.append(tg_id)

        return ids

    def is_admin(self, tg_id: int | None) -> bool:
        return tg_id is not None and tg_id in self.admin_id_list

    def network_has_credentials(self, key: str) -> bool:
        if key in ("bsc", "eth"):
            return bool(self.etherscan_api_key.strip()) or bool(self.rpc_urls(key))
        elif key == "tron":
            return bool(self.trongrid_api_key.strip())
        elif key == "ton":
            return bool(self.tonapi_key.strip())
        elif key == "sol":
            return bool(self.solana_rpc_url.strip())
        return False
        
    def rpc_urls(self, network: str) -> list[str]:
        urls_str = ""
        if network == "bsc":
            urls_str = self.bsc_rpc_urls
        elif network == "eth":
            urls_str = self.eth_rpc_urls
        return [u.strip() for u in urls_str.split(",") if u.strip()]

    def wallet_from_env(self, key: str) -> str:
        if key == "bsc":
            return self.wallet_bsc
        elif key == "eth":
            return self.wallet_eth
        elif key == "tron":
            return self.wallet_tron
        elif key == "ton":
            return self.wallet_ton
        elif key == "sol":
            return self.wallet_sol
        return ""


settings = Settings()

PLANS = {
    "14d": {"days": 14, "price_usd": Decimal("19"), "title": "14 днів"},
    "1m": {"days": 30, "price_usd": Decimal("29"), "title": "1 Місяць"}
}

INVOICE_TTL_MINUTES = 30

# --- Зіставлення сум ---
#
# Ідентифікація платежу тримається на унікальному «хвості» суми (29.4600,
# 29.4800, ...). Щоб хвіст працював, вікно допуску має бути ВУЗЬКИМ за крок
# сітки — інакше та сама сума підходить кільком інвойсам одночасно і платіж
# може дістатися чужому користувачу.
#
# Звідси два різні режими:
#   * автосканер знає лише суму, тому зіставляє строго (AUTO_MATCH_WINDOW),
#     а все неоднозначне відправляє в orphan_payments на ручний розбір;
#   * перевірка за хешем знає конкретний інвойс (людина сама його відкрила),
#     тому там можна бути щедрим: недоплата до MAX_UNDERPAY_USD прощається,
#     переплата не обмежена взагалі.

# Крок сітки унікальних сум. 0.02 USDT -> 49 одночасних інвойсів на мережу.
AMOUNT_STEP = Decimal("0.02")

# Наскільки сума транзакції може відхилятись, щоб автосканер вважав її «тією
# самою». Має бути помітно менше за AMOUNT_STEP / 2.
AUTO_MATCH_WINDOW = Decimal("0.005")

# Недоплата, яку прощаємо при перевірці за хешем. Покриває комісію бірж, які
# знімають її з суми виводу (BSC ~0.29 USDT).
MAX_UNDERPAY_USD = Decimal("0.50")

# Переплата доступ не блокує (людина отримує підписку), але понад цю суму
# сповіщаємо адмінів — щоб було з чим повертати, якщо клієнт попросить.
OVERPAY_ALERT_USD = Decimal("1")

SCANNER_INTERVAL_SEC = 40
MAX_HASH_ATTEMPTS = 5
HASH_COOLDOWN_SEC = 20
INVITE_TTL_HOURS = 24
