import os
import re
import shutil
import asyncio
import urllib.parse
from typing import Dict, Any, Optional, List
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next import Message, MessageType
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher

class MprisModule:
    """极速响应版 MPRIS 媒体监听（去阻塞、零延时封面同步、原生穿透置顶）"""

    def __init__(self, bus: MessageBus, state: IslandState, dispatcher: IpcDispatcher):
        self.bus = bus
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="MPRIS")
        self._ticker_task: Optional[asyncio.Task] = None
        self._last_emitted_state = ("", "", "", 0.0, "")

    def _unwrap_variant(self, variant: Any) -> Any:
        if hasattr(variant, 'value'):
            return self._unwrap_variant(variant.value)
        if isinstance(variant, list):
            return [self._unwrap_variant(item) for item in variant]
        if isinstance(variant, dict):
            return {k: self._unwrap_variant(v) for k, v in variant.items()}
        return variant

    async def init(self):
        await self._setup_listeners()
        await self.discover_players()
        self._ticker_task = asyncio.create_task(self._progress_ticker())

    async def close(self):
        if self._ticker_task and not self._ticker_task.done():
            self._ticker_task.cancel()

    async def _fetch_position(self) -> float:
        if not self.state.active_player:
            return self.state.position
        try:
            reply = await self.bus.call(
                Message(
                    destination=self.state.active_player,
                    path="/org/mpris/MediaPlayer2",
                    interface="org.freedesktop.DBus.Properties",
                    member="Get",
                    signature="ss",
                    body=["org.mpris.MediaPlayer2.Player", "Position"]
                )
            )
            if reply.message_type != MessageType.ERROR and reply.body:
                pos_us = self._unwrap_variant(reply.body[0])
                return round(float(pos_us) / 1_000_000.0, 2)
        except Exception:
            pass
        return self.state.position

    async def discover_players(self) -> List[str]:
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="ListNames"
                )
            )
            players = [n for n in reply.body[0] if n.startswith("org.mpris.MediaPlayer2.")]
            if players:
                if not self.state.active_player or self.state.active_player not in players:
                    await self.attach_player(players[0])
                return players
            else:
                self._clear_player_state()
                return []
        except Exception as e:
            self.log.error(f"播放器扫描异常: {e}")
            return []

    def _clear_player_state(self):
        self.state.active_player = None
        self.state.playback_status = "Stopped"
        self.state.title = "等待播放媒体..."
        self.state.artist = "空闲"
        self.state.art_url = ""
        self.state.position = 0.0
        self.state.duration = 0.0
        self._last_emitted_state = ("", "", "", 0.0, "")
        self.dispatcher.broadcast("MEDIA_UPDATE")

    async def attach_player(self, bus_name: str):
        if not bus_name:
            return
        self.state.active_player = bus_name
        try:
            reply = await self.bus.call(
                Message(
                    destination=bus_name,
                    path="/org/mpris/MediaPlayer2",
                    interface="org.freedesktop.DBus.Properties",
                    member="GetAll",
                    signature="s",
                    body=["org.mpris.MediaPlayer2.Player"]
                )
            )
            if reply.message_type == MessageType.ERROR:
                return

            all_props = self._unwrap_variant(reply.body[0])
            self.state.playback_status = all_props.get("PlaybackStatus", "Stopped")

            if "Metadata" in all_props:
                self._parse_metadata(all_props["Metadata"])

            self._last_emitted_state = (
                self.state.title, self.state.artist, self.state.playback_status, self.state.duration, self.state.art_url
            )
            self.dispatcher.broadcast("MEDIA_UPDATE")
            self.state.position = await self._fetch_position()
        except Exception as e:
            self.log.error(f"绑定播放器 {bus_name} 异常: {e}")

    def _parse_metadata(self, meta: Dict[str, Any]):
        self.state.title = meta.get("xesam:title", "未知曲目")
        artists = meta.get("xesam:artist", ["未知艺术家"])
        self.state.artist = ", ".join(artists) if isinstance(artists, list) else str(artists)
        
        art_url = meta.get("mpris:artUrl", "") or meta.get("xesam:url", "")
        if art_url:
            art_url = str(art_url)
            if art_url.startswith("file://"):
                art_url = "file://" + urllib.parse.unquote(art_url[7:])
            elif art_url.startswith("/") and not art_url.startswith("file://"):
                art_url = "file://" + art_url
        self.state.art_url = art_url

        length_us = meta.get("mpris:length", 0)
        self.state.duration = round(float(length_us) / 1_000_000.0, 1) if length_us else 0.0

    async def _setup_listeners(self):
        mpris_rule = "type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path='/org/mpris/MediaPlayer2'"
        name_rule = "type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged'"

        def signal_handler(msg: Message):
            if msg.member == "NameOwnerChanged" and msg.body:
                name, old_owner, new_owner = msg.body
                if name.startswith("org.mpris.MediaPlayer2."):
                    if new_owner and not old_owner:
                        asyncio.create_task(self.attach_player(name))
                    elif old_owner and not new_owner:
                        if self.state.active_player == name or self.state.active_player == old_owner:
                            asyncio.create_task(self.discover_players())
                return

            if msg.interface == "org.freedesktop.DBus.Properties" and msg.member == "PropertiesChanged":
                interface_name, changed_props, _ = msg.body
                if interface_name == "org.mpris.MediaPlayer2.Player":
                    unwrapped = self._unwrap_variant(changed_props)
                    sender_bus = msg.sender

                    new_status = unwrapped.get("PlaybackStatus")
                    if not self.state.active_player or new_status == "Playing":
                        if sender_bus and self.state.active_player != sender_bus:
                            self.state.active_player = sender_bus

                    if new_status:
                        self.state.playback_status = new_status

                    if "Metadata" in unwrapped:
                        self._parse_metadata(unwrapped["Metadata"])
                        self.state.position = 0.0

                    curr_state = (
                        self.state.title,
                        self.state.artist,
                        self.state.playback_status,
                        self.state.duration,
                        self.state.art_url
                    )

                    if curr_state != self._last_emitted_state:
                        self._last_emitted_state = curr_state
                        self.dispatcher.broadcast("MEDIA_UPDATE")

        for rule in [mpris_rule, name_rule]:
            await self.bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[rule]
                )
            )
        self.bus.add_message_handler(signal_handler)

    async def _progress_ticker(self):
        while True:
            await asyncio.sleep(1.0)
            if self.state.playback_status == "Playing" and self.state.active_player:
                self.state.position = await self._fetch_position()
                self.dispatcher.broadcast("MEDIA_PROGRESS")

    async def control(self, action: str):
        if not self.state.active_player:
            players = await self.discover_players()
            if not players or not self.state.active_player:
                return

        try:
            await self.bus.call(
                Message(
                    destination=self.state.active_player,
                    path="/org/mpris/MediaPlayer2",
                    interface="org.mpris.MediaPlayer2.Player",
                    member=action
                )
            )
        except Exception as e:
            self.log.error(f"执行媒体指令失败: {e}")

    # =========================================================================
    # 🚀 核心重构：真正可用的 PID + 标题双重精准唤醒置顶
    # =========================================================================
    async def _get_player_pid(self, bus_name: str) -> Optional[int]:
        # 1. 尝试从 bus 名称中直接提取 instance<PID>
        if m := re.search(r"instance(\d+)", bus_name):
            return int(m.group(1))

        # 2. 通过 D-Bus 获取 Unix 进程 PID
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="GetConnectionUnixProcessID",
                    signature="s",
                    body=[bus_name]
                )
            )
            if reply.message_type != MessageType.ERROR and reply.body:
                return int(reply.body[0])
        except Exception:
            pass
        return None

    async def raise_player(self) -> bool:
        """真正强制唤醒置顶当前正在播放的音乐窗口"""
        target_bus = self.state.active_player
        if not target_bus:
            players = await self.discover_players()
            target_bus = self.state.active_player if players else None

        if not target_bus:
            self.log.warning("未检测到活跃的媒体播放器，无法执行聚焦")
            return False

        self.log.info(f"🎯 尝试激活播放器: {target_bus}")

        # 步骤 1: 礼貌性发送 MPRIS Raise（Chromium 会忽略此协议，但原生播放器有效）
        try:
            await self.bus.call(
                Message(
                    destination=target_bus,
                    path="/org/mpris/MediaPlayer2",
                    interface="org.mpris.MediaPlayer2",
                    member="Raise"
                )
            )
        except Exception:
            pass

        # 步骤 2: 获取底层真实 PID，通过合成器强制对焦
        pid = await self._get_player_pid(target_bus)
        self.log.info(f"🔍 解析到播放器宿主 PID: {pid}")

        # 方案 A: 针对 Hyprland 合成器
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            if pid:
                cmd = f"hyprctl dispatch focuswindow pid:{pid}"
                proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                await proc.communicate()
                if proc.returncode == 0:
                    self.log.success(f"已通过 Hyprland 按 PID {pid} 置顶窗口")
                    return True

            # 若 PID 属于子进程，按当前曲名精准置顶
            if self.state.title and self.state.title != "等待播放媒体...":
                clean_title = re.escape(self.state.title.split(" - ")[0].strip()[:10])
                cmd = f"hyprctl dispatch focuswindow 'title:^(.*{clean_title}.*)$'"
                proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                await proc.communicate()
                if proc.returncode == 0:
                    self.log.success(f"已通过 Hyprland 按曲名 [{clean_title}] 置顶窗口")
                    return True

        # 方案 B: 针对 Sway 合成器
        if os.environ.get("SWAYSOCK"):
            if pid:
                cmd = f"swaymsg '[pid={pid}] focus'"
                proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.communicate()
                if proc.returncode == 0:
                    self.log.success(f"已通过 Sway 按 PID {pid} 置顶窗口")
                    return True

        # 方案 C: 针对 X11 / XWayland 环境
        if pid and shutil.which("xdotool"):
            cmd = f"WID=$(xdotool search --pid {pid} 2>/dev/null | tail -n 1); [ -n \"$WID\" ] && xdotool windowactivate --sync \"$WID\""
            proc = await asyncio.create_subprocess_shell(cmd)
            await proc.communicate()
            if proc.returncode == 0:
                self.log.success(f"已通过 xdotool 按 PID {pid} 置顶窗口")
                return True

        if pid and shutil.which("wmctrl"):
            # 扫描 wmctrl -lp 中的对应 PID
            proc = await asyncio.create_subprocess_exec("wmctrl", "-lp", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            for line in out.decode(errors="ignore").splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2] == str(pid):
                    wid = parts[0]
                    await asyncio.create_subprocess_exec("wmctrl", "-i", "-a", wid)
                    self.log.success(f"已通过 wmctrl -i -a {wid} 置顶窗口")
                    return True

        # 方案 D: 曲名兜底激活
        if self.state.title and self.state.title != "等待播放媒体...":
            clean_title = self.state.title.split(" - ")[0].strip()
            if shutil.which("wmctrl"):
                proc = await asyncio.create_subprocess_shell(f'wmctrl -a "{clean_title}"')
                await proc.communicate()
                if proc.returncode == 0:
                    self.log.success(f"已通过 wmctrl 曲名置顶窗口: {clean_title}")
                    return True

        self.log.warning("未能置顶音乐窗口（请确认播放器未被最小化至后台托盘）")
        return False