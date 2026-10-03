import shutil
import asyncio
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next import Message, MessageType
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher

class BrightnessModule:
    """双模式亮度控制：优先 KDE Solid D-Bus，非 KDE 环境自动降级至 brightnessctl"""

    def __init__(self, bus: MessageBus, state: IslandState, dispatcher: IpcDispatcher):
        self.bus = bus
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="BRIGHTNESS")
        self.backend_type = "none"

        self._max_brightness = 100
        self.service = "org.kde.Solid.PowerManagement"
        self.path = "/org/kde/Solid/PowerManagement/Actions/BrightnessControl"
        self.iface = "org.kde.Solid.PowerManagement.Actions.BrightnessControl"

    async def init(self):
        # 1. 尝试使用 KDE 协议
        if await self._try_init_kde():
            self.backend_type = "kde"
            self.log.success(f"亮度后端选定: KDE Solid D-Bus (当前亮度: {self.state.brightness}%)")
            return

        # 2. 降级尝试 brightnessctl
        if shutil.which("brightnessctl"):
            self.backend_type = "brightnessctl"
            await self._sync_brightnessctl()
            self.log.success(f"亮度后端选定: 通用 brightnessctl (当前亮度: {self.state.brightness}%)")
            return

        self.log.warning("未检测到 KDE Solid 或 brightnessctl，亮度控制功能停用。")

    async def _try_init_kde(self) -> bool:
        try:
            max_r = await self.bus.call(Message(destination=self.service, path=self.path, interface=self.iface, member="brightnessMax"))
            if max_r.message_type == MessageType.ERROR:
                return False
            self._max_brightness = max_r.body[0]

            cur_r = await self.bus.call(Message(destination=self.service, path=self.path, interface=self.iface, member="brightness"))
            if cur_r.message_type == MessageType.ERROR:
                return False

            self.state.brightness = int((cur_r.body[0] / self._max_brightness) * 100)

            rule = f"type='signal',interface='{self.iface}',member='brightnessChanged'"
            def on_brightness_changed(msg: Message):
                if msg.interface == self.iface and msg.member == "brightnessChanged":
                    self.state.brightness = int((msg.body[0] / self._max_brightness) * 100)
                    self.dispatcher.broadcast("BRIGHTNESS_UPDATE")

            await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus", interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[rule]))
            self.bus.add_message_handler(on_brightness_changed)
            return True
        except Exception:
            return False

    async def _sync_brightnessctl(self):
        try:
            proc = await asyncio.create_subprocess_exec("brightnessctl", "g", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            cur_out, _ = await proc.communicate()
            proc_m = await asyncio.create_subprocess_exec("brightnessctl", "m", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            max_out, _ = await proc_m.communicate()

            cur_val = float(cur_out.decode().strip())
            max_val = float(max_out.decode().strip())
            if max_val > 0:
                self.state.brightness = int(round((cur_val / max_val) * 100))
        except Exception as e:
            self.log.debug(f"brightnessctl 同步异常: {e}")

    async def set_delta(self, delta_percent: int):
        new_percent = max(1, min(100, self.state.brightness + delta_percent))
        await self.set_brightness(new_percent)

    async def set_brightness(self, target_percent: int):
        target = max(1, min(100, int(target_percent)))

        if self.backend_type == "kde":
            raw_val = int((target / 100.0) * self._max_brightness)
            try:
                await self.bus.call(
                    Message(destination=self.service, path=self.path, interface=self.iface, member="setBrightness", signature="i", body=[raw_val])
                )
                self.state.brightness = target
                self.log.success(f"已设置屏幕亮度 -> {target}%")
                self.dispatcher.broadcast("BRIGHTNESS_UPDATE")
            except Exception as e:
                self.log.error(f"KDE 设置亮度失败: {e}")

        elif self.backend_type == "brightnessctl":
            try:
                proc = await asyncio.create_subprocess_exec("brightnessctl", "s", f"{target}%")
                await proc.communicate()
                self.state.brightness = target
                self.log.success(f"已设置屏幕亮度 (brightnessctl) -> {target}%")
                self.dispatcher.broadcast("BRIGHTNESS_UPDATE")
            except Exception as e:
                self.log.error(f"brightnessctl 设置亮度失败: {e}")