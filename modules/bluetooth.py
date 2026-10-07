# ==========================================
# 📄 File: modules/bluetooth.py
# 🌟 完善版 BlueZ 蓝牙模块（补齐单设备连接与断开原生 D-Bus 控制）
# ==========================================
import asyncio
from typing import Dict, Any, Optional
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next import Message, MessageType, Variant
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher


class BluetoothModule:
    def __init__(self, bus: MessageBus, state: IslandState, dispatcher: IpcDispatcher):
        self.bus = bus
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="BLUETOOTH")
        self.adapter_path = "/org/bluez/hci0"

    def _unwrap_variant(self, variant: Any) -> Any:
        if hasattr(variant, 'value'):
            return self._unwrap_variant(variant.value)
        if isinstance(variant, list):
            return [self._unwrap_variant(item) for item in variant]
        if isinstance(variant, dict):
            return {k: self._unwrap_variant(v) for k, v in variant.items()}
        return variant

    async def init(self):
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects"
                )
            )
            if reply.message_type == MessageType.ERROR:
                self.log.warning("BlueZ 服务未就绪，蓝牙模块已禁用。")
                return

            objects = self._unwrap_variant(reply.body[0])
            self._sync_all_devices(objects)
            await self._setup_listeners()
            self.log.success(f"蓝牙模块就绪，已配对设备数: {len(self.state.bt_devices)}")
        except Exception as e:
            self.log.warning(f"蓝牙模块初始化异常: {e}")

    def _sync_all_devices(self, objects: Dict[str, Any]):
        devices = []
        for path, interfaces in objects.items():
            if "org.bluez.Device1" in interfaces:
                dev = interfaces["org.bluez.Device1"]
                battery = None
                if "org.bluez.Battery1" in interfaces:
                    battery = interfaces["org.bluez.Battery1"].get("Percentage", None)

                name = dev.get("Name", dev.get("Alias", "未知设备"))
                is_connected = dev.get("Connected", False)
                mac = dev.get("Address", "")

                devices.append({
                    "path": path,
                    "name": name,
                    "mac": mac,
                    "address": mac,
                    "connected": is_connected,
                    "icon": dev.get("Icon", "audio-card"),
                    "battery": battery
                })

        # 已连接设备排在前面
        devices.sort(key=lambda d: not d["connected"])
        self.state.bt_devices = devices

    async def _setup_listeners(self):
        prop_rule = "type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path_namespace='/org/bluez'"
        iface_rule = "type='signal',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesAdded',path='/'"

        def on_dbus_signal(msg: Message):
            if msg.member == "InterfacesAdded":
                path, interfaces = msg.body
                unwrapped = self._unwrap_variant(interfaces)
                if "org.bluez.Battery1" in unwrapped:
                    asyncio.create_task(self._refresh("BLUETOOTH_BATTERY"))
            elif msg.member == "PropertiesChanged":
                iface, changed_props, _ = msg.body
                unwrapped = self._unwrap_variant(changed_props)
                if iface == "org.bluez.Device1" and "Connected" in unwrapped:
                    conn = unwrapped["Connected"]
                    asyncio.create_task(self._on_connection_change(conn))
                if iface == "org.bluez.Battery1" and "Percentage" in unwrapped:
                    asyncio.create_task(self._refresh("BLUETOOTH_BATTERY"))

        for rule in [prop_rule, iface_rule]:
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
        self.bus.add_message_handler(on_dbus_signal)

    async def _on_connection_change(self, is_connected: bool):
        await self._refresh("BLUETOOTH_CONNECT" if is_connected else "BLUETOOTH_DISCONNECT")
        if is_connected:
            await asyncio.sleep(1.5)
            await self._refresh("BLUETOOTH_BATTERY_DELAYED")

    async def _refresh(self, event_name: str):
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects"
                )
            )
            self._sync_all_devices(self._unwrap_variant(reply.body[0]))
            self.dispatcher.broadcast(event_name)
        except Exception as e:
            self.log.error(f"刷新蓝牙设备状态失败: {e}")

    async def toggle_power(self):
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.bluez",
                    path=self.adapter_path,
                    interface="org.freedesktop.DBus.Properties",
                    member="Get",
                    signature="ss",
                    body=["org.bluez.Adapter1", "Powered"]
                )
            )
            new_state = not self._unwrap_variant(reply.body[0])
            await self.bus.call(
                Message(
                    destination="org.bluez",
                    path=self.adapter_path,
                    interface="org.freedesktop.DBus.Properties",
                    member="Set",
                    signature="ssv",
                    body=["org.bluez.Adapter1", "Powered", Variant("b", new_state)]
                )
            )
            self.state.bt_powered = new_state
            self.log.success(f"已切换蓝牙适配器电源状态 -> {new_state}")
            self.dispatcher.broadcast("BLUETOOTH_POWER")
        except Exception as e:
            self.log.error(f"切换蓝牙电源状态失败: {e}")

    # =========================================================================
    # 🚀 补齐：原生 BlueZ D-Bus 设备连接与断开接口
    # =========================================================================
    def _mac_to_device_path(self, mac: str) -> str:
        # BlueZ 对象路径格式: /org/bluez/hci0/dev_XX_XX_XX_XX_XX_XX
        dev_str = "dev_" + mac.replace(":", "_").upper()
        return f"{self.adapter_path}/{dev_str}"

    async def connect_device(self, mac: str) -> bool:
        """原生连接指定 MAC 蓝牙设备"""
        dev_path = self._mac_to_device_path(mac)
        self.log.info(f"🔗 请求连接蓝牙设备: {mac} (路径: {dev_path})")
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.bluez",
                    path=dev_path,
                    interface="org.bluez.Device1",
                    member="Connect"
                )
            )
            if reply.message_type != MessageType.ERROR:
                self.log.success(f"设备 [{mac}] 连接成功。")
                await self._refresh("BLUETOOTH_CONNECT")
                return True
            else:
                self.log.error(f"设备 [{mac}] 连接失败: {reply.error_name}")
                return False
        except Exception as e:
            self.log.error(f"连接设备异常: {e}")
            return False

    async def disconnect_device(self, mac: str) -> bool:
        """原生断开指定 MAC 蓝牙设备"""
        dev_path = self._mac_to_device_path(mac)
        self.log.info(f"🔌 请求断开蓝牙设备: {mac} (路径: {dev_path})")
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.bluez",
                    path=dev_path,
                    interface="org.bluez.Device1",
                    member="Disconnect"
                )
            )
            if reply.message_type != MessageType.ERROR:
                self.log.success(f"设备 [{mac}] 断开连接成功。")
                await self._refresh("BLUETOOTH_DISCONNECT")
                return True
            else:
                self.log.error(f"设备 [{mac}] 断开失败: {reply.error_name}")
                return False
        except Exception as e:
            self.log.error(f"断开设备异常: {e}")
            return False