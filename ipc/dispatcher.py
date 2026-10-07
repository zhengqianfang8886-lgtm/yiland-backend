# ==========================================
# 📄 File: ipc/dispatcher.py
# 🌟 双模 IPC 服务分发引擎（WebSocket + Unix Domain Socket 支持）
# ==========================================
import os
import json
import asyncio
from typing import Set
from loguru import logger
import websockets
from core.state import IslandState


def get_default_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.exists(runtime_dir):
        return os.path.join(runtime_dir, "alcove_island.sock")
    return f"/tmp/alcove_island_{os.getuid()}.sock"


class IpcDispatcher:
    def __init__(self, state: IslandState):
        self.state = state
        self.backend = None
        self.ws_clients: Set[websockets.WebSocketServerProtocol] = set()
        self.uds_writers = set()
        self.uds_server = None
        self.socket_path = get_default_socket_path()
        self.log = logger.bind(module="IPC_ROUTER")

    def set_backend(self, backend):
        self.backend = backend

    async def start_server(self, host: str = "127.0.0.1", port: int = 8765):
        # 1. 启动 WebSocket 服务
        async def ws_handler(websocket):
            self.ws_clients.add(websocket)
            self.log.success(f"灵动岛前端 UI (WebSocket) 已接入: {websocket.remote_address}")

            if self.backend and self.backend.mpris:
                self.state.position = await self.backend.mpris._fetch_position()

            await websocket.send(json.dumps(self.state.to_json("FULL_SYNC")))
            try:
                async for raw_msg in websocket:
                    await self._handle_client_command(raw_msg)
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                self.ws_clients.remove(websocket)
                self.log.info(f"灵动岛前端 UI (WebSocket) 断开连接: {websocket.remote_address}")

        await websockets.serve(ws_handler, host, port)
        self.log.success(f"WebSocket IPC 服务已就绪: ws://{host}:{port}")

        # 2. 🚀 启动 Unix Domain Socket (UDS) 双通道服务
        try:
            if os.path.exists(self.socket_path):
                try: os.remove(self.socket_path)
                except Exception: pass

            async def uds_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
                self.uds_writers.add(writer)
                self.log.success("灵动岛前端 UI (Unix Socket) 已接入。")
                writer.write((json.dumps(self.state.to_json("FULL_SYNC")) + "\n").encode())
                await writer.drain()

                try:
                    while line := await reader.readline():
                        raw_str = line.decode().strip()
                        if raw_str:
                            await self._handle_client_command(raw_str)
                except Exception:
                    pass
                finally:
                    self.uds_writers.discard(writer)
                    writer.close()
                    self.log.info("灵动岛前端 UI (Unix Socket) 断开连接。")

            self.uds_server = await asyncio.start_unix_server(uds_handler, path=self.socket_path)
            self.log.success(f"Unix Domain Socket 服务已就绪: {self.socket_path}")
        except Exception as e:
            self.log.warning(f"启动 UDS 服务异常: {e}")

    async def close(self):
        """释放 socket 文件"""
        if self.uds_server:
            self.uds_server.close()
            await self.uds_server.wait_closed()
        if os.path.exists(self.socket_path):
            try: os.remove(self.socket_path)
            except Exception: pass

    async def _handle_client_command(self, raw_msg: str):
        try:
            req = json.loads(raw_msg)
            action = req.get("action")
            self.log.info(f"接收到 UI 指令请求: {action}")
            if not self.backend:
                return

            # 媒体播放
            if action == "MEDIA_PLAY_PAUSE":
                if self.backend.mpris: await self.backend.mpris.control("PlayPause")
            elif action == "MEDIA_NEXT":
                if self.backend.mpris: await self.backend.mpris.control("Next")
            elif action == "MEDIA_PREV":
                if self.backend.mpris: await self.backend.mpris.control("Previous")
            elif action == "MEDIA_FOCUS":
                if self.backend.mpris: await self.backend.mpris.raise_player()

            # 音量、亮度与麦克风
            elif action == "VOLUME_DELTA":
                if self.backend.audio: await self.backend.audio.set_delta(req.get("delta", 5))
            elif action == "VOLUME_SET":
                if self.backend.audio: await self.backend.audio.set_volume(req.get("value", 50))
            elif action == "VOLUME_MUTE":
                if self.backend.audio: await self.backend.audio.toggle_mute()
            elif action == "MIC_DELTA":
                if self.backend.audio: await self.backend.audio.set_mic_delta(req.get("delta", 5))
            elif action == "MIC_MUTE_TOGGLE":
                if self.backend.audio: await self.backend.audio.toggle_mic_mute()
            elif action == "BRIGHTNESS_DELTA":
                if self.backend.brightness: await self.backend.brightness.set_delta(req.get("delta", 10))
            elif action == "BRIGHTNESS_SET":
                if self.backend.brightness: await self.backend.brightness.set_brightness(req.get("value", 50))

            # 蓝牙操作
            elif action == "BLUETOOTH_POWER_TOGGLE":
                if self.backend.bluetooth: await self.backend.bluetooth.toggle_power()
            elif action == "BLUETOOTH_CONNECT_DEVICE":
                mac = req.get("mac")
                if mac and self.backend.bluetooth:
                    await self.backend.bluetooth.connect_device(mac)
            elif action == "BLUETOOTH_DISCONNECT_DEVICE":
                mac = req.get("mac")
                if mac and self.backend.bluetooth:
                    await self.backend.bluetooth.disconnect_device(mac)
            elif action == "BLUETOOTH_SETTINGS_OPEN":
                if self.backend.power: await self.backend.power._safe_exec("blueman-manager || gnome-control-center bluetooth &")

            # 虚拟工作区
            elif action == "WORKSPACE_SWITCH":
                raw_target = req.get("target")
                target = raw_target if raw_target is not None else req.get("index")
                if target is not None and self.backend.workspace:
                    await self.backend.workspace.switch_workspace(target)
            elif action == "WORKSPACE_NEXT":
                if self.backend.workspace: await self.backend.workspace.switch_workspace("next")
            elif action == "WORKSPACE_PREV":
                if self.backend.workspace: await self.backend.workspace.switch_workspace("prev")

            # Teams 消息与通话
            elif action == "TEAMS_ANSWER":
                if self.backend.notifications: await self.backend.notifications.answer_call()
            elif action == "TEAMS_REJECT":
                if self.backend.notifications: await self.backend.notifications.reject_call()
            elif action == "TEAMS_FOCUS":
                if self.backend.notifications: await self.backend.notifications.focus_teams()

            # 系统电源、节能模式与系统监视器
            elif action == "POWER_SAVE_TOGGLE":
                if self.backend.power: await self.backend.power.toggle_power_save_mode()
            elif action == "POWER_SETTINGS_OPEN":
                if self.backend.power: await self.backend.power.open_power_settings()
            elif action == "SYSTEM_MONITOR_OPEN":
                if self.backend.power: await self.backend.power.open_system_monitor()
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
        payload = json.dumps(self.state.to_json(event_type))

        # 广播给 WebSocket 客户端
        if self.ws_clients:
            websockets.broadcast(self.ws_clients, payload)

        # 广播给 Unix Domain Socket 客户端
        if self.uds_writers:
            msg_bytes = (payload + "\n").encode()
            for w in list(self.uds_writers):
                try:
                    w.write(msg_bytes)
                except Exception:
                    self.uds_writers.discard(w)