import json
from typing import Set
from loguru import logger
import websockets
from core.state import IslandState

class IpcDispatcher:
    def __init__(self, state: IslandState):
        self.state = state
        self.backend = None
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.log = logger.bind(module="IPC_WS")

    def set_backend(self, backend):
        self.backend = backend

    async def start_server(self, host: str = "127.0.0.1", port: int = 8765):
        async def handler(websocket):
            self.clients.add(websocket)
            self.log.success(f"灵动岛前端 UI 已接入: {websocket.remote_address}")
            
            # 如果已有活跃播放器，先拉取最新真实位置再全量下发给前端
            if self.backend and self.backend.mpris:
                self.state.position = await self.backend.mpris._fetch_position()

            # 建立连接时立即下发全量状态帧
            await websocket.send(json.dumps(self.state.to_json("FULL_SYNC")))
            try:
                async for raw_msg in websocket:
                    await self._handle_client_command(raw_msg)
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                self.clients.remove(websocket)
                self.log.info(f"灵动岛前端 UI 断开连接: {websocket.remote_address}")

        await websockets.serve(handler, host, port)
        self.log.success(f"WebSocket IPC 服务已就绪: ws://{host}:{port}")

    async def _handle_client_command(self, raw_msg: str):
        try:
            req = json.loads(raw_msg)
            action = req.get("action")
            self.log.info(f"接收到 UI 动作请求: {action}")
            if not self.backend:
                return

            # 1. 媒体播放控制
            if action == "MEDIA_PLAY_PAUSE":
                if self.backend.mpris: await self.backend.mpris.control("PlayPause")
            elif action == "MEDIA_NEXT":
                if self.backend.mpris: await self.backend.mpris.control("Next")
            elif action == "MEDIA_PREV":
                if self.backend.mpris: await self.backend.mpris.control("Previous")
            
            # 2. 音量与亮度
            elif action == "VOLUME_DELTA":
                if self.backend.audio: await self.backend.audio.set_delta(req.get("delta", 5))
            elif action == "VOLUME_SET":
                if self.backend.audio: await self.backend.audio.set_volume(req.get("value", 50))
            elif action == "VOLUME_MUTE":
                if self.backend.audio: await self.backend.audio.toggle_mute()
            elif action == "BRIGHTNESS_DELTA":
                if self.backend.brightness: await self.backend.brightness.set_delta(req.get("delta", 10))
            elif action == "BRIGHTNESS_SET":
                if self.backend.brightness: await self.backend.brightness.set_brightness(req.get("value", 50))

            # 3. 蓝牙控制
            elif action == "BLUETOOTH_POWER_TOGGLE":
                if self.backend.bluetooth: await self.backend.bluetooth.toggle_power()

            # 4. 虚拟桌面切换
            elif action == "WORKSPACE_SWITCH":
                raw_target = req.get("target")
                target = raw_target if raw_target is not None else req.get("index")
                if target is not None and self.backend.workspace:
                    await self.backend.workspace.switch_workspace(target)
            elif action == "WORKSPACE_NEXT":
                if self.backend.workspace:
                    await self.backend.workspace.switch_workspace("next")
            elif action == "WORKSPACE_PREV":
                if self.backend.workspace:
                    await self.backend.workspace.switch_workspace("prev")

            # 5. Teams 呼叫处理
            elif action == "TEAMS_ANSWER":
                if self.backend.notifications: await self.backend.notifications.answer_call()
            elif action == "TEAMS_REJECT":
                if self.backend.notifications: await self.backend.notifications.reject_call()
            elif action == "TEAMS_FOCUS":
                if self.backend.notifications: await self.backend.notifications.focus_teams()

            # 6. 系统电源与会话控制 (新增)
            elif action == "POWER_SUSPEND":
                if self.backend.power: await self.backend.power.suspend()
            elif action == "POWER_REBOOT":
                if self.backend.power: await self.backend.power.reboot()
            elif action in ("POWER_OFF", "POWER_SHUTDOWN"):
                if self.backend.power: await self.backend.power.power_off()
            elif action == "SESSION_LOCK":
                if self.backend.power: await self.backend.power.lock_screen()
            elif action == "SESSION_LOGOUT":
                if self.backend.power: await self.backend.power.logout()
            elif action == "SESSION_SWITCH_USER":
                if self.backend.power: await self.backend.power.switch_user()

        except Exception as e:
            self.log.error(f"处理 UI 命令失败: {e}")

    def broadcast(self, event_type: str = "UPDATE"):
        if not self.clients:
            return
        payload = json.dumps(self.state.to_json(event_type))
        websockets.broadcast(self.clients, payload)