"""Home Assistant 双向桥接客户端（REST API 与服务调用）。

职责契约：
- 设备层与 AI 决策层解耦：VMAPBL 负责视觉/语音/多步安全推理，HA 负责智能家居设备连接与看板；
- 非阻塞异常安全：网络超时、连接被拒或未配置时，记录日志并返回失败字典，绝不向上层场景抛异常；
- 支持调用 HA 服务（例如 light.turn_on、switch.turn_off、climate.set_temperature）；
- 支持同步 VMAPBL 感知状态（例如向 HA 汇报 binary_sensor.vmapbl_occupancy、binary_sensor.vmapbl_fall）；
- 支持读取 HA 实体状态（例如查询 door_sensor、bed_occupancy、温湿度传感器）。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from core import config

log = logging.getLogger(__name__)

_DEFAULT_CLIENT: HomeAssistantClient | None = None


def ha_config() -> dict[str, Any]:
    """读取 Home Assistant 联合配置（settings.yaml 结合环境变量覆盖）。"""
    cfg = dict(config.settings().get("homeassistant") or {})
    env_url = (os.getenv("HA_BASE_URL") or os.getenv("HA_URL") or "").strip()
    if env_url:
        cfg["base_url"] = env_url
    env_token = (os.getenv("HA_TOKEN") or os.getenv("HOMEASSISTANT_TOKEN") or "").strip()
    if env_token:
        cfg["token"] = env_token
    return cfg


class HomeAssistantClient:
    """Home Assistant REST API 异步客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_s: float | None = None,
        enabled: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        cfg = ha_config()
        self.base_url = (base_url or cfg.get("base_url", "http://127.0.0.1:8123")).rstrip("/")
        self.token = (token if token is not None else cfg.get("token", "")).strip()
        self.timeout_s = float(
            cfg.get("timeout_s", 5.0) if timeout_s is None else timeout_s
        )
        self.enabled = bool(cfg.get("enabled", False) if enabled is None else enabled)
        self._transport = transport

    def is_configured(self) -> bool:
        """是否已配置连接地址与 Token。"""
        return bool(self.base_url and self.token)

    def is_available(self) -> bool:
        """是否已启用且配置完备。"""
        return self.enabled and self.is_configured()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "headers": self._headers(),
            "timeout": self.timeout_s,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def check_health(self) -> dict[str, Any]:
        """探活接口：GET /api/ 返回 HA 状态信息。"""
        if not self.is_configured():
            return {"available": False, "reason": "not_configured"}
        try:
            async with self._client() as client:
                resp = await client.get("/api/")
                if resp.status_code == 200:
                    data = resp.json() if resp.content else {}
                    return {"available": True, "message": data.get("message", "API running")}
                return {
                    "available": False,
                    "status_code": resp.status_code,
                    "reason": resp.text[:100],
                }
        except Exception as e:
            log.warning("Home Assistant 探活失败：%s", e)
            return {"available": False, "error": str(e)}

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 HA 服务（例如 light.turn_on、switch.turn_off、climate.set_temperature）。

        参数:
            domain: 服务域，例如 'light', 'switch', 'climate', 'script'
            service: 服务名，例如 'turn_on', 'turn_off', 'set_temperature'
            service_data: 传递给服务的附加参数字典
            target: 目标实体选择器，例如 {'entity_id': 'light.night_light'}
        """
        if not self.enabled:
            return {"ok": False, "reason": "ha_disabled"}
        if not self.is_configured():
            return {"ok": False, "reason": "ha_token_or_url_missing"}

        payload = dict(service_data or {})
        if target:
            payload.update(target)

        url = f"/api/services/{domain}/{service}"
        try:
            async with self._client() as client:
                resp = await client.post(url, json=payload)
                if resp.status_code in (200, 201):
                    res_json = resp.json() if resp.content else []
                    return {"ok": True, "result": res_json}
                log.warning(
                    "调用 HA 服务 %s/%s 失败（HTTP %d）：%s",
                    domain, service, resp.status_code, resp.text[:100]
                )
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error": resp.text[:200],
                }
        except Exception as e:
            log.warning("调用 HA 服务 %s/%s 异常：%s", domain, service, e)
            return {"ok": False, "error": str(e)}

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """读取指定实体的实时状态快照（GET /api/states/<entity_id>）。"""
        if not self.is_configured():
            return None
        url = f"/api/states/{entity_id}"
        try:
            async with self._client() as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception as e:
            log.warning("读取 HA 实体状态 %s 异常：%s", entity_id, e)
            return None

    async def set_state(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """上报或更新指定实体状态（POST /api/states/<entity_id>）。

        常用于把 VMAPBL 的本地感知事实同步给 Home Assistant 看板。
        """
        if not self.enabled or not self.is_configured():
            return {"ok": False, "reason": "ha_not_available"}

        payload: dict[str, Any] = {"state": str(state)}
        if attributes:
            payload["attributes"] = dict(attributes)

        url = f"/api/states/{entity_id}"
        try:
            async with self._client() as client:
                resp = await client.post(url, json=payload)
                if resp.status_code in (200, 201):
                    return {"ok": True, "data": resp.json()}
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error": resp.text[:200],
                }
        except Exception as e:
            log.warning("上报 HA 实体状态 %s 异常：%s", entity_id, e)
            return {"ok": False, "error": str(e)}


def default() -> HomeAssistantClient:
    """全局默认 Home Assistant 客户端实例。"""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = HomeAssistantClient()
    return _DEFAULT_CLIENT


def set_default(client: HomeAssistantClient | None) -> None:
    """注入测试打桩或定制客户端。"""
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client
