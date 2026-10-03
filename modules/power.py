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
    """
    系统级电源与会话管理模块：
    全面适配 SDDM、GDM、LightDM、KDE Plasma 5/6、Hyprland 等环境。
    涵盖：睡眠、重启、关机、锁屏、注销会话、切换用户。
    """

    def __init__(self, session_bus: Optional[MessageBus], system_bus: Optional[MessageBus], state: IslandState, dispatcher: IpcDispatcher):
        self.session_bus = session_bus
        self.system_bus = system_bus
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="POWER")

    async def init(self):
        self.log.success("电源与会话管理模块已就绪。")

    async def _safe_exec(self, cmd_str: str) -> bool:
        """执行底层外部命令并记录明确的日志"""
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
                self.log.debug(f"命令执行未成功 [{proc.returncode}]: {cmd_str} -> {err_msg}")
                return False
        except Exception as e:
            self.log.error(f"执行异常: {cmd_str} -> {e}")
            return False

    # =========================================================================
    # 1. 睡眠 (SUSPEND - 深度低功耗待机)
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
                    self.log.success("已通过 systemd-logind 触发睡眠。")
                    return True
            except Exception as e:
                self.log.debug(f"D-Bus 睡眠调用异常: {e}")

        return await self._safe_exec("systemctl suspend || loginctl suspend")

    # =========================================================================
    # 2. 重启 (REBOOT - 重新引导系统)
    # =========================================================================
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
                    self.log.success("已通过 systemd-logind 触发重启。")
                    return True
            except Exception as e:
                self.log.debug(f"D-Bus 重启调用异常: {e}")

        return await self._safe_exec("systemctl reboot || reboot")

    # =========================================================================
    # 3. 关机 (POWER OFF - 安全切断电源)
    # =========================================================================
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
                    self.log.success("已通过 systemd-logind 触发关机。")
                    return True
            except Exception as e:
                self.log.debug(f"D-Bus 关机调用异常: {e}")

        return await self._safe_exec("systemctl poweroff || poweroff")

    # =========================================================================
    # 4. 锁定屏幕 (LOCK - 快捷键 Win+L)
    # =========================================================================
    async def lock_screen(self) -> bool:
        self.log.info("🔒 触发系统操作: [锁定屏幕 / Lock]")
        # 1. 优先使用标准命令行工具进行锁屏
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

        # 2. Session D-Bus 兜底
        if self.session_bus:
            try:
                reply = await self.session_bus.call(
                    Message(
                        destination="org.freedesktop.ScreenSaver",
                        path="/ScreenSaver",
                        interface="org.freedesktop.ScreenSaver",
                        member="Lock"
                    )
                )
                if reply.message_type != MessageType.ERROR:
                    self.log.success("已通过 Session D-Bus ScreenSaver.Lock 锁定。")
                    return True
            except Exception as e:
                self.log.debug(f"ScreenSaver.Lock 异常: {e}")

        return False

    # =========================================================================
    # 5. 注销会话 (LOGOUT - 退出当前账户)
    # =========================================================================
    async def logout(self) -> bool:
        self.log.warning("🚪 触发系统操作: [注销会话 / Logout]")
        # 1. KDE Session D-Bus
        if self.session_bus:
            for srv, path, iface in [
                ("org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown"),
                ("org.kde.ksmserver", "/KSMServer", "org.kde.KSMServerInterface"),
            ]:
                try:
                    reply = await self.session_bus.call(
                        Message(
                            destination=srv,
                            path=path,
                            interface=iface,
                            member="logout",
                            signature="iii",
                            body=[0, 0, 0]
                        )
                    )
                    if reply.message_type != MessageType.ERROR:
                        self.log.success(f"已通过 {srv} 触发注销。")
                        return True
                except Exception:
                    pass

        # 2. 外部指令降级
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

    # =========================================================================
    # 6. 切换用户 (SWITCH - 调出登录管理器 Greeter)
    # =========================================================================
    async def switch_user(self) -> bool:
        self.log.info("👥 触发系统操作: [切换用户 / Switch User]")

        # 【通道 1】System Bus -> DisplayManager.Seat.SwitchToGreeter (SDDM / LightDM 官方正统接口)
        if self.system_bus:
            for seat_path in ["/org/freedesktop/DisplayManager/Seat0", "/org/freedesktop/DisplayManager/Seat/0"]:
                try:
                    reply = await self.system_bus.call(
                        Message(
                            destination="org.freedesktop.DisplayManager",
                            path=seat_path,
                            interface="org.freedesktop.DisplayManager.Seat",
                            member="SwitchToGreeter"
                        )
                    )
                    if reply.message_type != MessageType.ERROR:
                        self.log.success(f"已通过 DisplayManager ({seat_path}) 调起 Greeter 登录界面。")
                        return True
                except Exception as e:
                    self.log.debug(f"DisplayManager.SwitchToGreeter 调用尝试 [{seat_path}]: {e}")

        # 【通道 2】System Bus -> GDM (GNOME Display Manager 专属通道)
        if self.system_bus:
            try:
                reply = await self.system_bus.call(
                    Message(
                        destination="org.gnome.DisplayManager",
                        path="/org/gnome/DisplayManager/LocalDisplayFactory",
                        interface="org.gnome.DisplayManager.LocalDisplayFactory",
                        member="CreateTransientDisplay"
                    )
                )
                if reply.message_type != MessageType.ERROR:
                    self.log.success("已通过 GDM LocalDisplayFactory 唤出新登录界面。")
                    return True
            except Exception as e:
                self.log.debug(f"GDM CreateTransientDisplay 尝试失败: {e}")

        # 【通道 3】Session Bus -> KDE KRunner switchUser / KSMServer
        if self.session_bus:
            try:
                reply = await self.session_bus.call(
                    Message(
                        destination="org.kde.krunner",
                        path="/App",
                        interface="org.kde.krunner.App",
                        member="switchUser"
                    )
                )
                if reply.message_type != MessageType.ERROR:
                    self.log.success("已通过 KDE KRunner 触发 switchUser。")
                    return True
            except Exception as e:
                self.log.debug(f"KDE krunner switchUser 尝试失败: {e}")

        # 【通道 4】Linux 标准命令行指令阶梯调用
        # gdbus 是绝大多数 Linux 发行版(包含 Arch/Ubuntu/Fedora/openSUSE)的标配工具
        cli_candidates = [
            # 1. SDDM / LightDM 标准 gdbus 系统调用
            "gdbus call --system --dest org.freedesktop.DisplayManager --object-path /org/freedesktop/DisplayManager/Seat0 --method org.freedesktop.DisplayManager.Seat.SwitchToGreeter",
            # 2. GDM gdbus 系统调用
            "gdbus call --system --dest org.gnome.DisplayManager --object-path /org/gnome/DisplayManager/LocalDisplayFactory --method org.gnome.DisplayManager.LocalDisplayFactory.CreateTransientDisplay",
            # 3. SDDM / LightDM qdbus 调用
            "qdbus6 --system org.freedesktop.DisplayManager /org/freedesktop/DisplayManager/Seat0 org.freedesktop.DisplayManager.Seat.SwitchToGreeter",
            "qdbus --system org.freedesktop.DisplayManager /org/freedesktop/DisplayManager/Seat0 org.freedesktop.DisplayManager.Seat.SwitchToGreeter",
            # 4. KDE KRunner
            "qdbus6 org.kde.krunner /App switchUser",
            "qdbus org.kde.krunner /App switchUser",
            # 5. LightDM dm-tool
            "dm-tool switch-to-greeter",
            # 6. GDM flexiserver
            "gdmflexiserver"
        ]

        for cmd in cli_candidates:
            bin_name = cmd.split()[0]
            if shutil.which(bin_name):
                if await self._safe_exec(cmd):
                    return True

        # 【通道 5】终极兜底：唤出锁屏（锁屏界面上均有用户切换按钮）
        self.log.warning("所有 Greeter 直切通道均未响应，执行锁屏以供手动切换用户。")
        return await self.lock_screen()