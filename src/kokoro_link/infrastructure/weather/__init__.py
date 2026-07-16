"""Weather-context adapters。

提供 :class:`WeatherContextPort` 的具體實作。目前包含：

* :class:`OpenMeteoWeatherProvider` — 免 API key 的全球天氣來源。
* :class:`NullWeatherProvider` — 永遠回空字串，給 fake / disabled 模式。

跟 :mod:`kokoro_link.infrastructure.calendar` 對齊：純事實層、不寫死
行為、`describe()` 回 prompt-ready 中文片段。
"""
