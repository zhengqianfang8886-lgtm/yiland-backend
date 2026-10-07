# ==========================================
# 📄 File: modules/power.py
# 🌟 完善版电源模块（UPower 充电脉冲、节能模式切换与会话调度）
# ==========================================
import os
import shutil
import asyncio
from typing import Optional
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next import Message, MessageType, Variant
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher


class PowerModule:
    def __init__(self, session_bus: Optional[MessageBus], system_bus: Optional[MessageBus], state: IslandState, dispatcher: IpcDispatcher):
        self.session_bus = session_bus
        self.system_bus = system_bus
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="POWER")

    async def init(self):
        if self.system_bus:
            await self._setup_upower_listener()
            await self._sync_power_profile()
        self.log.success("电源与会话管理模块已就绪（UPower 零轮询电池感知已挂载）。")

    async def _safe_exec(self, cmd_str: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                self.log.success(f"命令执行成功: {cmd_str}")
                return True
            else:
                err_msg = stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
                self.log.debug(f"命令未成功 [{proc.returncode}]: {cmd_str} -> {err_msg}")
                return False
        except Exception as e:
            self.log.error(f"执行异常: {cmd_str} -> {e}")
            return False

    # =========================================================================
    # 🔋 原生 UPower 零轮询电池与插拔脉冲感知
    # =========================================================================
    async def _setup_upower_listener(self):
        try:
            reply = await self.system_bus.call(
                Message(
                    destination="org.freedesktop.UPower",
                    path="/org/freedesktop/UPower",
                    interface="org.freedesktop.UPower",
                    member="GetDisplayDevice"
                )
            )
            if reply.message_type == MessageType.ERROR:
                return

            device_path = reply.body[0]

            async def _read_props():
                prop_reply = await self.system_bus.call(
                    Message(
                        destination="org.freedesktop.UPower",
                        path=device_path,
                        interface="org.freedesktop.DBus.Properties",
                        member="GetAll",
                        signature="s",
                        body=["org.freedesktop.UPower.Device"]
                    )
                )
                if prop_reply.message_type != MessageType.ERROR:
                    props = prop_reply.body[0]
                    # State: 1=Charging, 2=Discharging, 4=Fully Charged
                    state_code = props.get("State", Variant("u", 0)).value
                    pct = int(round(props.get("Percentage", Variant("d", 100.0)).value))
                    is_charging = (state_code in (1, 4))

                    old_charging = self.state.battery_charging
                    self.state.battery_pct = pct
                    self.state.battery_charging = is_charging

                    # 🚀 检测插拔脉冲事件
                    if not old_charging and is_charging:
                        self.log.success(f"⚡ 电源线已接入 · 正在充电 (当前电量 {pct}%)")
                        self.dispatcher.broadcast("BATTERY_CHARGING_START")
                    elif old_charging and not is_charging:
                        self.log.warning(f"🔋 电源线已断开 · 电池供电模式 (当前电量 {pct}%)")
                        self.dispatcher.broadcast("BATTERY_DISCHARGING_START")
                    else:
                        self.dispatcher.broadcast("BATTERY_UPDATE")

            await _read_props()

            rule = f"type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path='{device_path}'"
            await self.system_bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[rule]
                )
            )

            def _on_props_changed(msg: Message):
                if msg.path == device_path and msg.member == "PropertiesChanged":
                    asyncio.create_task(_read_props())

            self.system_bus.add_message_handler(_on_props_changed)
            self.log.success(f"🔋 成功接入 UPower 监听: 初始电量 {self.state.battery_pct}% (充电: {self.state.battery_charging})")

        except Exception as e:
            self.log.debug(f"UPower 挂载失败: {e}")

    # =========================================================================
    # 🍃 电源模式切换
    # =========================================================================
    async def _sync_power_profile(self):
        try:
            if shutil.which("powerprofilesctl"):
                proc = await asyncio.create_subprocess_exec("powerprofilesctl", "get", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await proc.communicate()
                self.state.power_profile = out.decode().strip() or "balanced"
        except Exception:
            pass

    async def toggle_power_save_mode(self) -> bool:
        target = "balanced" if self.state.power_profile == "power-saver" else "power-saver"
        self.log.info(f"🍃 请求切换电源配置 -> {target}")

        if shutil.which("powerprofilesctl"):
            if await self._safe_exec(f"powerprofilesctl set {target}"):
                self.state.power_profile = target
                self.dispatcher.broadcast("POWER_PROFILE_UPDATE")
                return True

        if shutil.which("tlp"):
            cmd = "tlp bat" if target == "power-saver" else "tlp ac"
            if await self._safe_exec(cmd):
                self.state.power_profile = target
                self.dispatcher.broadcast("POWER_PROFILE_UPDATE")
                return True

        return False

    async def open_power_settings(self):
        settings_cmds = [
            "gnome-control-center power",
            "plasma-open-settings powerdevilprofilesconfig",
            "xfce4-power-manager-settings",
            "mate-power-preferences"
        ]
        for cmd in settings_cmds:
            bin_name = cmd.split()[0]
            if shutil.which(bin_name):
                await self._safe_exec(f"{cmd} &")
                return

    async def open_system_monitor(self):
        mon_cmds = [
            "mission-center", "resources", "gnome-system-monitor",
            "plasma-systemmonitor", "mate-system-monitor", "xfce4-taskmanager"
        ]
        for cmd in mon_cmds:
            if shutil.which(cmd):
                await self._safe_exec(f"{cmd} &")
                return

    # =========================================================================
    # 会话控制
    # =========================================================================
    async def suspend(self) -> bool:
        self.log.warning("💤 触发系统操作: [睡眠 / Suspend]")
        if self.system_bus:
            try:
                reply = await self.system_bus.call(
                    Message(
                        destination="org.freedesktop.login1",
                        path="/org/freedesktop/login1",
                        interface="org.freedesktop.login1.Manager",
                        member="Suspend",
                        signature="b",
                        body=[Variant("b", True)]
                    )
                )
                if reply.message_type != MessageType.ERROR:
                    return True
            except Exception:
                pass
        return await self._safe_exec("systemctl suspend || loginctl suspend")

    async def reboot(self) -> bool:
        self.log.warning("🔄 触发系统操作: [重启 / Reboot]")
        if self.system_bus:
            try:
                reply = await self.system_bus.call(
                    Message(
                        destination="org.freedesktop.login1",
                        path="/org/freedesktop/login1",
                        interface="org.freedesktop.login1.Manager",
                        member="Reboot",
                        signature="b",
                        body=[Variant("b", True)]
                    )
                )
                if reply.message_type != MessageType.ERROR:
                    return True
            except Exception:
                pass
        return await self._safe_exec("systemctl reboot || reboot")

    async def power_off(self) -> bool:
        self.log.warning("⏻ 触发系统操作: [关机 / Power Off]")
        if self.system_bus:
            try:
                reply = await self.system_bus.call(
                    Message(
                        destination="org.freedesktop.login1",
                        path="/org/freedesktop/login1",
                        interface="org.freedesktop.login1.Manager",
                        member="PowerOff",
                        signature="b",
                        body=[Variant("b", True)]
                    )
                )
                if reply.message_type != MessageType.ERROR:
                    return True
            except Exception:
                pass
        return await self._safe_exec("systemctl poweroff || poweroff")

    async def lock_screen(self) -> bool:
        self.log.info("🔒 触发系统操作: [锁定屏幕 / Lock]")
        lock_cmds = [
            "loginctl lock-session",
            "qdbus6 org.freedesktop.ScreenSaver /ScreenSaver Lock",
            "qdbus org.freedesktop.ScreenSaver /ScreenSaver Lock",
            "hyprlock",
            "swaylock -f",
            "waylock",
            "xdg-screensaver lock"
        ]
        for cmd in lock_cmds:
            bin_name = cmd.split()[0]
            if shutil.which(bin_name):
                if await self._safe_exec(cmd):
                    return True
        return False

    async def logout(self) -> bool:
        self.log.warning("🚪 触发系统操作: [注销会话 / Logout]")
        session_id = os.environ.get("XDG_SESSION_ID")
        cmd_loginctl = f"loginctl terminate-session {session_id}" if session_id else "loginctl terminate-session ${XDG_SESSION_ID:-self}"
        logout_cmds = [
            "qdbus6 org.kde.Shutdown /Shutdown logout 0 0 0",
            "qdbus org.kde.Shutdown /Shutdown logout 0 0 0",
            cmd_loginctl,
            "hyprctl dispatch exit"
        ]
        for cmd in logout_cmds:
            bin_name = cmd.split()[0]
            if shutil.which(bin_name):
                if await self._safe_exec(cmd):
                    return True
        return False

    async def switch_user(self) -> bool:
        self.log.info("👥 触发系统操作: [切换用户 / Switch User]")
        cli_candidates = [
            "gdbus call --system --dest org.freedesktop.DisplayManager --object-path /org/freedesktop/DisplayManager/Seat0 --method org.freedesktop.DisplayManager.Seat.SwitchToGreeter",
            "gdbus call --system --dest org.gnome.DisplayManager --object-path /org/gnome/DisplayManager/LocalDisplayFactory --method org.gnome.DisplayManager.LocalDisplayFactory.CreateTransientDisplay",
            "dm-tool switch-to-greeter"
        ]
        for cmd in cli_candidates:
            bin_name = cmd.split()[0]
            if shutil.which(bin_name):
                if await self._safe_exec(cmd):
                    return True
        return await self.lock_screen()