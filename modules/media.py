import asyncio
import shutil
import time
from loguru import logger
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher

class MediaModule:
    # 隔离符号，防止曲名、歌手中含常规标点导致解析分裂
    DELIMITER = "<|SPLIT|>"

    def __init__(self, state: IslandState, dispatcher: IpcDispatcher):
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="MEDIA")
        self.has_playerctl = False

    async def init(self):
        if not shutil.which("playerctl"):
            self.log.warning("'playerctl' not found. Media timeline and control disabled.")
            return

        self.has_playerctl = True
        self.log.info("Media backend: playerctl (MPRIS over D-Bus)")

        # 启动时先拉取一次完整状态与时间基准
        await self._sync_metadata()
        await self._sync_position()
        self.log.success(f"Initial Media: [{self.state.playback_status}] {self.state.title} ({self.state.position:.1f}/{self.state.duration:.1f}s)")

        # 启动后台守护任务
        asyncio.create_task(self._listen_mpris_events())
        asyncio.create_task(self._timeline_ticker())

    async def _sync_metadata(self):
        """同步歌曲信息（标题、歌手、总时长、封面URL、播放器来源）"""
        try:
            # 格式: 播放器名;;状态;;时长(微秒);;歌曲名;;艺术家;;封面URL
            fmt = f"{{{{playerName}}}}{self.DELIMITER}{{{{status}}}}{self.DELIMITER}{{{{mpris:length}}}}{self.DELIMITER}{{{{title}}}}{self.DELIMITER}{{{{artist}}}}{self.DELIMITER}{{{{mpris:artUrl}}}}"
            proc = await asyncio.create_subprocess_exec(
                "playerctl", "metadata", "--format", fmt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            text = out.decode().strip()

            if not text:
                # 系统中当前没有运行任何播放器，重置为空闲默认值
                self.state.active_player = None
                self.state.playback_status = "Stopped"
                self.state.title = "等待播放媒体..."
                self.state.artist = "空闲"
                self.state.art_url = ""
                self.state.duration = 0.0
                self.state.position = 0.0
                return

            parts = text.split(self.DELIMITER)
            self.state.active_player = parts[0] if len(parts) > 0 else None
            self.state.playback_status = parts[1] if len(parts) > 1 else "Stopped"
            
            # MPRIS length 为微秒 (μs)，转换成秒
            raw_len = parts[2] if len(parts) > 2 else "0"
            self.state.duration = float(raw_len) / 1_000_000.0 if raw_len.isdigit() else 0.0

            self.state.title = parts[3] if len(parts) > 3 and parts[3] else "未知曲目"
            self.state.artist = parts[4] if len(parts) > 4 and parts[4] else "未知艺术家"
            self.state.art_url = parts[5] if len(parts) > 5 else ""

        except Exception as e:
            self.log.debug(f"Sync media metadata error: {e}")

    async def _sync_position(self):
        """拉取物理基准时间戳，用于本地推算"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "playerctl", "position",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            text = out.decode().strip()

            if text:
                self.state.position = float(text)
                self.state._last_sync_time = time.monotonic()
        except Exception as e:
            self.log.debug(f"Sync position error: {e}")

    async def _listen_mpris_events(self):
        """
        利用 playerctl -F 挂起监听 MPRIS D-Bus 事件：
        在【切歌】、【暂停/播放切换】、【拖动进度条(Seek)】时会立即捕获
        """
        while True:
            try:
                # 监听状态、时间轴变化、歌曲信息变化
                proc = await asyncio.create_subprocess_exec(
                    "playerctl", "-F", "metadata", "--format", "{{status}}::{{position}}::{{title}}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                
                while line := await proc.stdout.readline():
                    if line:
                        await self._sync_metadata()
                        await self._sync_position()
                        # 通知前端/客户端：媒体核心状态有变动
                        self.dispatcher.broadcast(self.state.to_json("MEDIA_UPDATE"))

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.warning(f"Playerctl subscriber error: {e}, reconnecting in 3s...")
                await asyncio.sleep(3)

    async def _timeline_ticker(self):
        """
        以 1 秒一次的低频时钟节拍，在播放时驱动灵动岛前端进度条和秒数递增走字
        """
        while True:
            try:
                await asyncio.sleep(1.0)
                if self.state.playback_status == "Playing" and self.state.duration > 0:
                    # 将实时计算好瞬时 position 的完整包广播给前端/灵动岛 UI
                    self.dispatcher.broadcast(self.state.to_json("MEDIA_TICK"))
            except asyncio.CancelledError:
                break

    # ================= 播放器交互控制接口 =================

    async def play_pause(self):
        """播放/暂停切换"""
        if not self.has_playerctl: return
        proc = await asyncio.create_subprocess_exec("playerctl", "play-pause")
        await proc.wait()
        await self._sync_metadata()
        await self._sync_position()
        self.dispatcher.broadcast(self.state.to_json("MEDIA_UPDATE"))

    async def next(self):
        """下一首"""
        if not self.has_playerctl: return
        proc = await asyncio.create_subprocess_exec("playerctl", "next")
        await proc.wait()

    async def previous(self):
        """上一首"""
        if not self.has_playerctl: return
        proc = await asyncio.create_subprocess_exec("playerctl", "previous")
        await proc.wait()

    async def seek(self, delta_seconds: float):
        """快进 / 快退（例如 +5 或 -5 秒）"""
        if not self.has_playerctl: return
        op = f"{delta_seconds}+" if delta_seconds > 0 else f"{abs(delta_seconds)}-"
        proc = await asyncio.create_subprocess_exec("playerctl", "position", op)
        await proc.wait()
        await self._sync_position()
        self.dispatcher.broadcast(self.state.to_json("MEDIA_TICK"))

    async def set_position(self, target_seconds: float):
        """跳转到绝对秒数（拖动进度条松手时触发）"""
        if not self.has_playerctl: return
        proc = await asyncio.create_subprocess_exec("playerctl", "position", str(target_seconds))
        await proc.wait()
        await self._sync_position()
        self.dispatcher.broadcast(self.state.to_json("MEDIA_TICK"))