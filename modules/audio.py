# FILE: modules/audio.py
import re
import time
import shutil
import asyncio
from typing import Optional
from loguru import logger
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher

class AudioModule:
    """生产级高敏音频模块：原生子进程保障 + 无感任务折叠 + 零排队延迟"""

    def __init__(self, state: IslandState, dispatcher: IpcDispatcher):
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="AUDIO")
        self.engine: Optional[str] = None

        self._listener_task: Optional[asyncio.Task] = None
        self._poller_task: Optional[asyncio.Task] = None

        # 防排队折叠锁机制
        self._sync_lock = asyncio.Lock()
        self._pending_sync = False
        self._pending_target = "@DEFAULT_SINK@"

        self._last_event_time = 0.0
        self._is_internal_updating = False

    async def init(self) -> None:
        if shutil.which("pactl"):
            self.engine = "pactl"
            self.log.info("音频引擎选定: pactl (PulseAudio / PipeWire)")
        elif shutil.which("wpctl"):
            self.engine = "wpctl"
            self.log.info("音频引擎选定: wpctl (WirePlumber)")
        else:
            self.log.error("未检测到 pactl 或 wpctl，音频模块停用！")
            return

        # 1. 打印挂载设备
        await self._diagnose_sinks()

        # 2. 初始校准
        await self._sync_volume_once(target_sink="@DEFAULT_SINK@", silent=True)

        # 3. 启动基于 stdbuf 的底层事件监听（解耦非阻塞）
        self._listener_task = asyncio.create_task(self._listen_hardware_events())

        # 4. 启动兜底巡检（闲时自动校验）
        self._poller_task = asyncio.create_task(self._fallback_poller())

        self.log.success(f"音频模块就绪 | 初始主音量: {self.state.volume}% | 静音: {self.state.is_muted}")

    async def close(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
        if self._poller_task and not self._poller_task.done():
            self._poller_task.cancel()

    async def _run_cmd(self, *args: str) -> str:
        """使用安全的原生子进程调用，杜绝 shell 兼容性与超时断流"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=0.8)
            return stdout.decode(errors="ignore").strip()
        except Exception:
            return ""

    async def _diagnose_sinks(self):
        if self.engine != "pactl":
            return
        out = await self._run_cmd("pactl", "list", "short", "sinks")
        if out:
            self.log.info("📢 系统已挂载的音频输出设备 (Sinks):")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    is_blue = "🎧 [外接/蓝牙]" if "bluez" in parts[1].lower() else "🔊 [内置/声卡]"
                    self.log.info(f"   -> ID #{parts[0]}: {parts[1]} {is_blue}")

    async def _sync_volume_once(self, target_sink: str = "@DEFAULT_SINK@", silent: bool = False) -> bool:
        """核心状态同步执行器"""
        old_vol = self.state.volume
        old_mute = self.state.is_muted
        cur_vol = old_vol
        cur_mute = old_mute

        try:
            if self.engine == "pactl":
                # 依然使用经过验证的稳定并发拉取
                vol_out, mute_out = await asyncio.gather(
                    self._run_cmd("pactl", "get-sink-volume", target_sink),
                    self._run_cmd("pactl", "get-sink-mute", target_sink)
                )
                if m := re.search(r"(\d+)%", vol_out):
                    cur_vol = int(m.group(1))
                if mute_out:
                    cur_mute = "yes" in mute_out.lower()

            elif self.engine == "wpctl":
                wp_out = await self._run_cmd("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
                if m := re.search(r"Volume:\s+([0-9.]+)", wp_out):
                    cur_vol = int(round(float(m.group(1)) * 100))
                cur_mute = "[MUTED]" in wp_out

            changed = (old_vol != cur_vol) or (old_mute != cur_mute)
            self.state.volume = cur_vol
            self.state.is_muted = cur_mute

            if changed and not silent:
                st = "🔇 已静音" if cur_mute else "🔊 开启"
                self.log.info(f"🎧 [音量变动捕捉: 设备 {target_sink}] 音量: {cur_vol}% | 状态: {st}")
                self.dispatcher.broadcast("VOLUME_UPDATE")
                return True

        except Exception as e:
            self.log.warning(f"读取音量状态异常: {e}")

        return False

    def _trigger_coalesced_sync(self, target: str):
        """核心去延迟逻辑：无论硬件事件发得多猛，绝不排队，折叠合并"""
        self._last_event_time = time.monotonic()
        self._pending_target = target

        if not self._sync_lock.locked():
            # 没有正在查询的任务，立即启动
            asyncio.create_task(self._process_coalesced_sync())
        else:
            # 前一个查询还没结束，仅立标，不新建进程排队
            self._pending_sync = True

    async def _process_coalesced_sync(self):
        """折叠消费者：只取第一次和最终态"""
        async with self._sync_lock:
            while True:
                self._pending_sync = False
                target = self._pending_target
                
                await self._sync_volume_once(target_sink=target, silent=False)
                
                # 如果查询期间没有新的连击事件进来，直接结束；如果有，立刻以最新态补查一次
                if not self._pending_sync:
                    break

    async def _listen_hardware_events(self) -> None:
        """事件读取循环：彻底移除了内部 await，微秒级收包"""
        if self.engine != "pactl":
            return

        cmd = ["pactl", "subscribe"]
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-oL", "-eL", "pactl", "subscribe"]

        while True:
            proc: Optional[asyncio.subprocess.Process] = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )

                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break

                    decoded = line.decode(errors="ignore").strip().lower()

                    if ("sink" in decoded or "server" in decoded or "card" in decoded) and "change" in decoded:
                        if self._is_internal_updating:
                            continue

                        target = "@DEFAULT_SINK@"
                        if m := re.search(r"sink\s+#(\d+)", decoded):
                            target = m.group(1)

                        # 关键改造：微秒级派发，绝不在此处 await 卡死下个事件的接收
                        self._trigger_coalesced_sync(target)

            except asyncio.CancelledError:
                if proc and proc.returncode is None:
                    proc.terminate()
                break
            except Exception as e:
                self.log.warning(f"事件流连接波动，1秒后自愈: {e}")
                await asyncio.sleep(1)
            finally:
                if proc and proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass

    async def _fallback_poller(self) -> None:
        """智能兜底巡检：按键时主动避让，不抢总线；静止时安全巡检"""
        while True:
            try:
                await asyncio.sleep(1.0)
                # 如果过去 1.5 秒内发生过按键，事件监听工作完美，巡检主动跳过
                if (time.monotonic() - self._last_event_time) < 1.5:
                    continue

                if not self._is_internal_updating:
                    await self._sync_volume_once(target_sink="@DEFAULT_SINK@", silent=False)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.warning(f"巡检异常: {e}")

    # ================= 软件端交互接口（鼠标滑块/CLI 调整） =================

    async def set_delta(self, delta_percent: int) -> None:
        if not self.engine:
            return
        self._is_internal_updating = True
        try:
            new_vol = max(0, min(150, self.state.volume + delta_percent))
            self.state.volume = new_vol
            self.dispatcher.broadcast("VOLUME_UPDATE")
            self.log.info(f"🖱️ [软件调音] 音量 -> {new_vol}%")

            if self.engine == "pactl":
                op = f"+{delta_percent}%" if delta_percent > 0 else f"{delta_percent}%"
                asyncio.create_task(self._run_cmd("pactl", "set-sink-volume", "@DEFAULT_SINK@", op))
            elif self.engine == "wpctl":
                val = abs(delta_percent) / 100.0
                op = f"{val}+" if delta_percent > 0 else f"{val}-"
                asyncio.create_task(self._run_cmd("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", op))
        finally:
            await asyncio.sleep(0.05)
            self._is_internal_updating = False

    async def set_volume(self, target_percent: int) -> None:
        if not self.engine:
            return
        self._is_internal_updating = True
        try:
            target = max(0, min(150, int(target_percent)))
            self.state.volume = target
            self.dispatcher.broadcast("VOLUME_UPDATE")
            self.log.info(f"🎚️ [设定绝对音量] -> {target}%")

            if self.engine == "pactl":
                asyncio.create_task(self._run_cmd("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{target}%"))
            elif self.engine == "wpctl":
                val = round(target / 100.0, 2)
                asyncio.create_task(self._run_cmd("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", str(val)))
        finally:
            await asyncio.sleep(0.05)
            self._is_internal_updating = False

    async def toggle_mute(self) -> None:
        if not self.engine:
            return
        self._is_internal_updating = True
        try:
            self.state.is_muted = not self.state.is_muted
            self.dispatcher.broadcast("VOLUME_UPDATE")
            st = "已静音" if self.state.is_muted else "已取消静音"
            self.log.info(f"🔇 [切换静音] -> {st}")

            if self.engine == "pactl":
                asyncio.create_task(self._run_cmd("pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"))
            elif self.engine == "wpctl":
                asyncio.create_task(self._run_cmd("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"))
        finally:
            await asyncio.sleep(0.05)
            self._is_internal_updating = False