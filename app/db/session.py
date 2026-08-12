import contextlib
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings
from app.db.models import Base

if settings.database_url.startswith("sqlite"):
    engine = create_async_engine(settings.database_url, echo=False)
else:
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True
    )

async_session = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

@contextlib.asynccontextmanager
async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session

async def init_db():
    """Створити відсутні таблиці.

    Раніше це робилося тільки для SQLite, і на Postgres бот стартував у порожню
    базу — перший же запит падав. Міграцій у проєкті поки немає (каталог
    versions/ порожній), тож create_all — єдиний спосіб отримати схему.
    Операція ідемпотентна: наявні таблиці не чіпаються.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def close_db():
    await engine.dispose()
