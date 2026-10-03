import asyncio
import time
from typing import Dict, Any, Tuple
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next import Message
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher

class NetworkModule:
    def __init__(self, bus: MessageBus, state: IslandState, dispatcher: IpcDispatcher):
        self.bus = bus
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="NETWORK")
        
        self.last_rx_bytes = 0
        self.last_tx_bytes = 0
        self.last_calc_time = 0.0

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
            await self._sync_network_and_vpn()
            await self._setup_listeners()
            self.log.success(
                f"网络模块就绪: 连通={self.state.net_connected} ({self.state.net_name}) | "
                f"VPN={self.state.vpn_active} ({self.state.vpn_name or '无'})"
            )
            asyncio.create_task(self._speed_monitor_loop())
        except Exception as e:
            self.log.warning(f"网络/VPN 模块初始化异常: {e}")

    async def _sync_network_and_vpn(self):
        try:
            reply = await self.bus.call(
                Message(
                    destination="org.freedesktop.NetworkManager",
                    path="/org/freedesktop/NetworkManager",
                    interface="org.freedesktop.DBus.Properties",
                    member="Get",
                    signature="ss",
                    body=["org.freedesktop.NetworkManager", "State"]
                )
            )
            state_val = self._unwrap_variant(reply.body[0])
            self.state.net_connected = (state_val == 70)

            reply = await self.bus.call(
                Message(
                    destination="org.freedesktop.NetworkManager",
                    path="/org/freedesktop/NetworkManager",
                    interface="org.freedesktop.DBus.Properties",
                    member="Get",
                    signature="ss",
                    body=["org.freedesktop.NetworkManager", "ActiveConnections"]
                )
            )
            active_paths = self._unwrap_variant(reply.body[0])

            vpn_found = False
            primary_net_found = False

            for path in active_paths:
                conn_reply = await self.bus.call(
                    Message(
                        destination="org.freedesktop.NetworkManager",
                        path=path,
                        interface="org.freedesktop.DBus.Properties",
                        member="GetAll",
                        signature="s",
                        body=["org.freedesktop.NetworkManager.Connection.Active"]
                    )
                )
                conn = self._unwrap_variant(conn_reply.body[0])
                c_id = conn.get("Id", "")
                c_type = conn.get("Type", "")
                is_vpn_flag = conn.get("Vpn", False)

                if is_vpn_flag or c_type in ["wireguard", "vpn", "tun"]:
                    self.state.vpn_active = True
                    self.state.vpn_name = c_id
                    self.state.vpn_type = c_type
                    vpn_found = True
                    continue

                if not primary_net_found:
                    if "wireless" in c_type:
                        self.state.net_name = c_id
                        self.state.net_type = "wifi"
                        primary_net_found = True
                        devices = conn.get("Devices", [])
                        if devices:
                            await self._sync_wifi_strength(devices[0])
                    elif "ethernet" in c_type:
                        self.state.net_name = c_id
                        self.state.net_type = "ethernet"
                        self.state.net_strength = 100
                        primary_net_found = True

            if not vpn_found:
                self.state.vpn_active = False
                self.state.vpn_name = ""
                self.state.vpn_type = ""

        except Exception as e:
            self.log.debug(f"拉取网络/VPN 状态异常: {e}")

    async def _sync_wifi_strength(self, device_path: str):
        try:
            ap_reply = await self.bus.call(
                Message(
                    destination="org.freedesktop.NetworkManager",
                    path=device_path,
                    interface="org.freedesktop.DBus.Properties",
                    member="Get",
                    signature="ss",
                    body=["org.freedesktop.NetworkManager.Device.Wireless", "ActiveAccessPoint"]
                )
            )
            ap_path = self._unwrap_variant(ap_reply.body[0])
            if ap_path and ap_path != "/":
                st_reply = await self.bus.call(
                    Message(
                        destination="org.freedesktop.NetworkManager",
                        path=ap_path,
                        interface="org.freedesktop.DBus.Properties",
                        member="Get",
                        signature="ss",
                        body=["org.freedesktop.NetworkManager.AccessPoint", "Strength"]
                    )
                )
                self.state.net_strength = int(self._unwrap_variant(st_reply.body[0]))
        except Exception:
            pass

    async def _setup_listeners(self):
        rule = (
            "type='signal',"
            "interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged',"
            "path='/org/freedesktop/NetworkManager'"
        )

        def on_nm_changed(msg: Message):
            if msg.interface == "org.freedesktop.DBus.Properties" and msg.member == "PropertiesChanged":
                _, changed_props, _ = msg.body
                unwrapped = self._unwrap_variant(changed_props)
                if "ActiveConnections" in unwrapped or "State" in unwrapped:
                    asyncio.create_task(self._handle_network_change())

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
        self.bus.add_message_handler(on_nm_changed)

    async def _handle_network_change(self):
        old_vpn_state = self.state.vpn_active
        old_vpn_name = self.state.vpn_name
        await self._sync_network_and_vpn()

        if old_vpn_state != self.state.vpn_active or old_vpn_name != self.state.vpn_name:
            if self.state.vpn_active:
                self.log.success(f"🛡️ VPN 接入 -> [{self.state.vpn_name}] ({self.state.vpn_type})")
                self.dispatcher.broadcast("VPN_CONNECTED")
            else:
                self.log.warning(f"⚠️ VPN 已断开 -> 原连接 [{old_vpn_name}]")
                self.dispatcher.broadcast("VPN_DISCONNECTED")
        else:
            self.dispatcher.broadcast("NETWORK_UPDATE")

    def _read_total_bytes(self) -> Tuple[int, int]:
        rx_total, tx_total = 0, 0
        try:
            with open("/proc/net/dev", "r") as f:
                lines = f.readlines()[2:]
                for line in lines:
                    cols = line.strip().split()
                    if cols[0].startswith("lo:"):
                        continue
                    rx_total += int(cols[1])
                    tx_total += int(cols[9])
        except Exception:
            pass
        return rx_total, tx_total

    def _format_speed(self, bytes_per_sec: float) -> str:
        if bytes_per_sec >= 1024 * 1024:
            return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
        elif bytes_per_sec >= 1024:
            return f"{bytes_per_sec / 1024:.0f} KB/s"
        else:
            return f"{int(bytes_per_sec)} B/s"

    async def _speed_monitor_loop(self):
        self.last_rx_bytes, self.last_tx_bytes = self._read_total_bytes()
        self.last_calc_time = time.time()

        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            dt = now - self.last_calc_time
            if dt <= 0:
                continue

            rx_cur, tx_cur = self._read_total_bytes()
            rx_speed = (rx_cur - self.last_rx_bytes) / dt
            tx_speed = (tx_cur - self.last_tx_bytes) / dt

            self.last_rx_bytes, self.last_tx_bytes = rx_cur, tx_cur
            self.last_calc_time = now

            self.state.net_rx_speed = self._format_speed(rx_speed)
            self.state.net_tx_speed = self._format_speed(tx_speed)