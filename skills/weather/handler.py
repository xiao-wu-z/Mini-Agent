"""Weather skill：返回固定城市的模拟天气。

边界：
    日期按上海时区计算，天气数值是固定测试数据，不代表实时天气或实际预报。
    不支持的城市返回明确错误，不生成似真的天气数据。
"""

from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from skills._shared.context import ToolContext
from skills._shared.errors import SkillError, SkillErrorCode

MAX_CITY_LENGTH = 80
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
DAY_OFFSETS = {"today": 0, "tomorrow": 1}
MOCK_WEATHER = {
    "北京": ("晴", 18, 10),
    "上海": ("多云", 22, 30),
    "杭州": ("小雨", 20, 80),
    "深圳": ("多云", 27, 40),
    "广州": ("晴", 28, 20),
}


class WeatherArgs(BaseModel):
    """城市名称和查询日期，工具描述必须说明仅支持模拟数据。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    city: str = Field(min_length=1, max_length=MAX_CITY_LENGTH)
    day: Literal["today", "tomorrow"] = "today"


async def weather(arguments: WeatherArgs, context: ToolContext) -> dict:
    """返回指定日期的固定样例天气；不支持的城市交给模型澄清。"""
    city = arguments.city.removesuffix("市")
    forecast = MOCK_WEATHER.get(city)
    if forecast is None:
        raise SkillError(
            SkillErrorCode.LOCATION_NOT_SUPPORTED,
            "Mock 天气仅支持北京、上海、杭州、深圳和广州，请指定其中一个城市。",
        )
    condition, temperature, precipitation_probability = forecast
    forecast_date = datetime.now(LOCAL_TIMEZONE).date() + timedelta(days=DAY_OFFSETS[arguments.day])
    return {
        "is_mock": True,
        "city": city,
        "date": forecast_date.isoformat(),
        "condition": condition,
        "temperature_celsius": temperature,
        "precipitation_probability_percent": precipitation_probability,
    }
