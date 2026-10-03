import asyncio
import urllib.parse
from typing import Dict, Any, Optional, List
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next import Message, MessageType
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher

class MprisModule:
    """极速响应版 MPRIS 媒体监听（去阻塞、零延时封面同步）"""

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

            # 进度拉取异步放在后台，不阻塞媒体元数据广播
            self.state.position = await self._fetch_position()
        except Exception as e:
            self.log.error(f"绑定播放器 {bus_name} 异常: {e}")

    def _parse_metadata(self, meta: Dict[str, Any]):
        self.state.title = meta.get("xesam:title", "未知曲目")
        artists = meta.get("xesam:artist", ["未知艺术家"])
        self.state.artist = ", ".join(artists) if isinstance(artists, list) else str(artists)
        
        # 极速提取封面 URL
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
                        # 🚀 核心修复：直接解析新元数据，绝不提前置空 art_url 引起前端闪烁
                        self._parse_metadata(unwrapped["Metadata"])
                        # 切歌时瞬时进度归零，绝不等待阻塞的 D-Bus 查询
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
                        # 🚀 立即广播，毫秒级推给灵动岛
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