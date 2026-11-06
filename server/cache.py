from redis.asyncio import Redis

from server.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password.get_secret_value() or None,
        ssl=settings.redis_ssl,
        socket_connect_timeout=settings.redis_timeout,
        socket_timeout=settings.redis_timeout,
        decode_responses=True,
    )
