# ==========================================
# 📄 File: core/backend.py
# 🌟 完善版核心微服务挂载中枢（优雅生命周期）
# ==========================================
import sys
from typing import Optional
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

from core.state import IslandState
from ipc.dispatcher import IpcDispatcher
from modules.mpris import MprisModule
from modules.brightness import BrightnessModule
from modules.audio import AudioModule
from modules.bluetooth import BluetoothModule
from modules.network import NetworkModule
from modules.notifications import NotificationModule
from modules.workspace import WorkspaceModule
from modules.power import PowerModule


class DynamicIslandBackend:
    def __init__(self):
        self.log = logger.bind(module="DAEMON")
        self.state = IslandState()
        self.dispatcher = IpcDispatcher(self.state)
        self.dispatcher.set_backend(self)

        self.session_bus: Optional[MessageBus] = None
        self.system_bus: Optional[MessageBus] = None

        self.workspace: Optional[WorkspaceModule] = None
        self.mpris: Optional[MprisModule] = None
        self.brightness: Optional[BrightnessModule] = None
        self.audio: Optional[AudioModule] = None
        self.bluetooth: Optional[BluetoothModule] = None
        self.network: Optional[NetworkModule] = None
        self.notifications: Optional[NotificationModule] = None
        self.power: Optional[PowerModule] = None

    async def initialize(self):
        self.log.info("正在建立 D-Bus 总线连接...")
        try:
            self.session_bus = await MessageBus(bus_type=BusType.SESSION).connect()
            self.log.success("Session Bus 连接成功。")
        except Exception as e:
            self.log.critical(f"Session Bus 连接失败: {e}")
            sys.exit(1)

        try:
            self.system_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self.log.success("System Bus 连接成功。")
        except Exception as e:
            self.log.warning(f"无法接入 System Bus (蓝牙/网络/UPower 功能受限): {e}")

        # 1. 虚拟桌面模块
        self.workspace = WorkspaceModule(self.session_bus, self.state, self.dispatcher)
        await self.workspace.init()

        # 2. 原生 MPRIS 媒体模块
        self.mpris = MprisModule(self.session_bus, self.state, self.dispatcher)
        await self.mpris.init()

        # 3. 屏幕亮度模块
        self.brightness = BrightnessModule(self.session_bus, self.state, self.dispatcher)
        await self.brightness.init()

        # 4. 音频模块 (PipeWire / WirePlumber)
        self.audio = AudioModule(self.state, self.dispatcher)
        await self.audio.init()

        # 5. 蓝牙与网络 (基于 System Bus)
        if self.system_bus:
            self.bluetooth = BluetoothModule(self.system_bus, self.state, self.dispatcher)
            await self.bluetooth.init()

            self.network = NetworkModule(self.system_bus, self.state, self.dispatcher)
            await self.network.init()

        # 6. 通知监听与呼叫模块
        self.notifications = NotificationModule(self.state, self.dispatcher)
        await self.notifications.init()

        # 7. 电源、节能模式与 UPower 电池感知模块
        self.power = PowerModule(self.session_bus, self.system_bus, self.state, self.dispatcher)
        await self.power.init()

        # 8. 启动双模 IPC 服务 (WebSocket + UDS)
        await self.dispatcher.start_server()

    async def shutdown(self):
        """优雅关闭释放系统资源"""
        self.log.info("正在释放系统资源...")
        if self.audio:
            await self.audio.close()
        if self.notifications:
            await self.notifications.close()
        if self.mpris:
            await self.mpris.close()
        if self.dispatcher:
            await self.dispatcher.close()