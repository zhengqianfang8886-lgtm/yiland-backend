# FILE: modules/notifications.py
import os
import re
import hashlib
import asyncio
import shutil
from typing import Dict, Any, Optional
from loguru import logger
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next import Message, MessageType
from core.state import IslandState
from ipc.dispatcher import IpcDispatcher

class TeamsFilterConfig:
    TEAMS_APP_NAMES = [
        "teams", "teams-for-linux", "microsoft-teams", "ms-teams",
        "google-chrome", "chrome", "chromium", "com.github.ismaelmartinez.teams_for_linux"
    ]
    TEAMS_DOMAINS = [
        "teams.live.com", "teams.microsoft.com", 
        "teams.cloud.microsoft", "teams.office.com"
    ]
    CALL_KEYWORDS = [
        "正在呼叫", "来电", "邀请您加入通话", "邀请你加入通话", 
        "邀请您加入视频", "视频通话", "语音通话",
        "calling", "incoming call", "incoming video call", "is calling"
    ]
    CALL_END_KEYWORDS = [
        "未接来电", "已取消呼叫", "通话已结束", "通话结束",
        "missed call", "call ended", "call cancelled"
    ]
    CLEANUP_REGEX = [
        (r"\s*[-—|]\s*(Microsoft Teams|Teams|Teams for Linux)$", ""),
        (r"^https?://[a-zA-Z0-9.-]+[:：]?\s*", ""),
        (r"<[^>]+>", ""),
    ]

class NotificationModule:
    """旁路嗅探全局通知，靶向识别 Teams 消息与来电呼叫，支持 X11/Wayland 智能唤醒与通话控制"""

    def __init__(self, state: IslandState, dispatcher: IpcDispatcher):
        self.state = state
        self.dispatcher = dispatcher
        self.log = logger.bind(module="TEAMS_NOTIFY")

        self.monitor_bus: Optional[MessageBus] = None
        self.action_bus: Optional[MessageBus] = None

        self.current_call_id: Optional[int] = None
        self._pending_call_serial: Optional[int] = None
        self.is_calling: bool = False
        self._call_timeout_task: Optional[asyncio.Task] = None

        self.cache_dir = os.path.expanduser("~/.cache/dynamic_island/avatars")
        os.makedirs(self.cache_dir, exist_ok=True)

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
            self.action_bus = await MessageBus(bus_type=BusType.SESSION).connect()
            self.log.info("Action D-Bus 连接成功。")
        except Exception as e:
            self.log.error(f"Action D-Bus 连接异常: {e}")

        asyncio.create_task(self._monitor_lifecycle_loop())

    async def close(self):
        if self._call_timeout_task and not self._call_timeout_task.done():
            self._call_timeout_task.cancel()

    async def _monitor_lifecycle_loop(self):
        while True:
            try:
                self.monitor_bus = await MessageBus(bus_type=BusType.SESSION).connect()
                
                rule_notify = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
                rule_closed = "type='signal',interface='org.freedesktop.Notifications',member='NotificationClosed'"
                rule_reply = "type='method_return',sender='org.freedesktop.Notifications'"

                await self.monitor_bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus.Monitoring",
                        member="BecomeMonitor",
                        signature="asu",
                        body=[[rule_notify, rule_closed, rule_reply], 0]
                    )
                )

                self.monitor_bus.send = lambda *args, **kwargs: None
                self.monitor_bus.add_message_handler(self._on_dbus_message)
                self.log.success("Teams 来电/通知旁路嗅探器已激活。")

                await self.monitor_bus.wait_for_disconnect()
                self.log.warning("通知嗅探器断开连接，2 秒后重连...")
            except Exception as e:
                self.log.error(f"通知嗅探器异常: {e}，将在 3 秒后重试...")
                await asyncio.sleep(3)

    def _on_dbus_message(self, msg: Message) -> bool:
        try:
            if msg.message_type == MessageType.METHOD_RETURN:
                if self.is_calling and self._pending_call_serial == msg.reply_serial:
                    if msg.body and len(msg.body) > 0:
                        self.current_call_id = msg.body[0]
                        self.log.info(f"捕获到呼叫通知真实 ID: {self.current_call_id}")
                        self._pending_call_serial = None
                return True

            if msg.member == "NotificationClosed":
                closed_id = msg.body[0]
                reason = msg.body[1] if len(msg.body) > 1 else 0
                if self.is_calling and (self.current_call_id == closed_id or self.current_call_id is None):
                    self._stop_call(f"NotificationClosed 触发 (原因代码: {reason})")
                return True

            if msg.member == "Notify":
                asyncio.create_task(self._handle_notify_async(msg))

        except Exception as e:
            self.log.debug(f"处理 D-Bus 报文异常: {e}")
        finally:
            return True

    async def _handle_notify_async(self, msg: Message):
        try:
            app_name = str(msg.body[0]).lower()
            replaces_id = msg.body[1]
            app_icon = str(msg.body[2])
            raw_title = str(msg.body[3])
            raw_content = str(msg.body[4])

            raw_hints = msg.body[6] if len(msg.body) > 6 else {}
            unwrapped_hints = self._unwrap_variant(raw_hints)
            hints_text = str(unwrapped_hints).lower()

            is_native = any(t in app_name for t in TeamsFilterConfig.TEAMS_APP_NAMES)
            is_web = any(d in hints_text for d in TeamsFilterConfig.TEAMS_DOMAINS)
            is_raw_match = any(d in str(msg.body).lower() for d in TeamsFilterConfig.TEAMS_DOMAINS)

            if not (is_native or is_web or is_raw_match):
                return

            clean_title = raw_title.strip()
            clean_content = raw_content.strip()
            for pattern, repl in TeamsFilterConfig.CLEANUP_REGEX:
                clean_title = re.sub(pattern, repl, clean_title, flags=re.IGNORECASE).strip()
                clean_content = re.sub(pattern, repl, clean_content, flags=re.IGNORECASE).strip()

            loop = asyncio.get_running_loop()
            avatar_path = await loop.run_in_executor(None, self._extract_and_save_avatar, app_icon, unwrapped_hints)
            if not avatar_path:
                avatar_path = await loop.run_in_executor(None, self._generate_letter_avatar, clean_title)
            if avatar_path:
                self.state.last_notify_avatar = avatar_path

            combined_text = f"{clean_title} {clean_content}".lower()
            is_call_ended_text = any(kw in combined_text for kw in TeamsFilterConfig.CALL_END_KEYWORDS)

            if is_call_ended_text:
                self._stop_call(f"收到挂断通知: {clean_content}")
                return

            is_calling_text = any(kw in combined_text for kw in TeamsFilterConfig.CALL_KEYWORDS)

            if is_calling_text:
                self.is_calling = True
                if replaces_id != 0:
                    self.current_call_id = replaces_id
                else:
                    self._pending_call_serial = msg.serial

                self.log.success(f"🚨 [Teams 来电呼叫] 呼叫方: {clean_title}")
                self.state.last_notify_title = clean_title
                self.state.last_notify_content = clean_content or "邀请加入通话..."
                self.state.last_notify_icon = "📞"
                self.dispatcher.broadcast("TEAMS_CALL_INCOMING")

                if self._call_timeout_task and not self._call_timeout_task.done():
                    self._call_timeout_task.cancel()
                self._call_timeout_task = asyncio.create_task(self._auto_call_watchdog(45))
                return

            self.state.last_notify_title = clean_title
            self.state.last_notify_content = clean_content
            self.state.last_notify_icon = "👥"
            self.dispatcher.broadcast("TEAMS_NOTIFICATION")

        except Exception as e:
            self.log.error(f"解析 Teams 通知失败: {e}")

    async def focus_teams(self) -> bool:
        """跨桌面唤醒置顶 Teams（全面支持 Hyprland、Sway、KDE Wayland 和 X11）"""
        self.log.info("🎯 正在搜寻并唤醒 Teams 窗口...")
        call_id = self.current_call_id or 0

        # 通道 1: 通过 FreeDesktop / KDE 通知 Action 尝试唤起
        if self.action_bus:
            try:
                msg = Message(
                    destination="org.freedesktop.Notifications",
                    path="/org/freedesktop/Notifications",
                    interface="org.kde.NotificationManager",
                    member="InvokeAction",
                    signature="us",
                    body=[call_id, "default"]
                )
                await self.action_bus.call(msg)
            except Exception:
                pass

        # 通道 2: Wayland 原生合成器判断与唤起
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            # Hyprland 原生激活
            cmd_hypr = 'hyprctl dispatch focuswindow "class:^(.*teams.*)$" || hyprctl dispatch focuswindow "title:^(.*Teams.*)$"'
            proc = await asyncio.create_subprocess_shell(cmd_hypr, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
            if proc.returncode == 0:
                self.log.success("已通过 Hyprland IPC 激活 Teams 窗口。")
                return True

        if os.environ.get("SWAYSOCK"):
            cmd_sway = 'swaymsg \'[app_id="(?i).*teams.*"] focus\' || swaymsg \'[class="(?i).*teams.*"] focus\''
            proc = await asyncio.create_subprocess_shell(cmd_sway, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
            if proc.returncode == 0:
                self.log.success("已通过 Sway IPC 激活 Teams 窗口。")
                return True

        # 通道 3: X11 / XWayland 环境支持
        if shutil.which("wmctrl"):
            cmd_wmctrl = 'wmctrl -a "Microsoft Teams" 2>/dev/null || wmctrl -a "teams.microsoft.com" 2>/dev/null'
            proc = await asyncio.create_subprocess_shell(cmd_wmctrl)
            await proc.communicate()
            if proc.returncode == 0:
                self.log.success("已通过 wmctrl 精准置顶 Teams 窗口。")
                return True

        if shutil.which("xdotool"):
            cmd_xdotool = '''
            WID=$(xdotool search --name "Microsoft Teams" 2>/dev/null | tail -n 1)
            if [ -n "$WID" ]; then
                xdotool windowactivate --sync "$WID"
                exit 0
            fi
            exit 1
            '''
            proc2 = await asyncio.create_subprocess_shell(cmd_xdotool)
            await proc2.communicate()
            if proc2.returncode == 0:
                self.log.success("已通过 xdotool 激活 Teams 窗口。")
                return True

        self.log.warning("未定位到 Teams 窗口或当前合成器不支持直接激活。")
        return False

    async def answer_call(self) -> bool:
        """安全接听：先聚焦，后模拟按键"""
        if not self.is_calling:
            self.log.warning("当前没有正在响铃的 Teams 来电。")
            return False

        focused = await self.focus_teams()
        if not focused:
            self.log.error("安全拦截：未能置顶 Teams 窗口，终止按键注入以防误触！")
            return False

        await asyncio.sleep(0.12)
        # 支持 xdotool (X11) 或 wtype (Wayland)
        cmd = "wtype -M ctrl -M shift -k s 2>/dev/null || xdotool key --clearmodifiers ctrl+shift+s 2>/dev/null"
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()

        self.log.success("📞 已触发接听快捷键！")
        if self._call_timeout_task and not self._call_timeout_task.done():
            self._call_timeout_task.cancel()
        return True

    async def reject_call(self) -> bool:
        """安全拒接/挂断"""
        if not self.is_calling:
            self.log.warning("当前没有正在响铃的 Teams 来电。")
            return False

        focused = await self.focus_teams()
        if not focused:
            self.log.error("安全拦截：未能置顶 Teams 窗口，执行强制重置状态。")
            self._stop_call("窗口激活失败重置")
            return False

        await asyncio.sleep(0.12)
        cmd = "wtype -M ctrl -M shift -k d 2>/dev/null || xdotool key --clearmodifiers ctrl+shift+d 2>/dev/null"
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()

        self._stop_call("用户主动拒接/挂断")
        return True

    def _stop_call(self, reason: str):
        if not self.is_calling:
            return
        self.is_calling = False
        self.current_call_id = None
        self._pending_call_serial = None
        if self._call_timeout_task and not self._call_timeout_task.done():
            self._call_timeout_task.cancel()

        self.log.info(f"📵 [Teams 通话结束] 原因: {reason}")
        self.state.last_notify_icon = "📵"
        self.state.last_notify_content = "呼叫已结束"
        self.dispatcher.broadcast("TEAMS_CALL_ENDED")

    async def _auto_call_watchdog(self, timeout_sec: int):
        await asyncio.sleep(timeout_sec)
        if self.is_calling:
            self._stop_call("超时无应答自动结束")

    def _clean_file_path(self, raw_path: str) -> str:
        p = str(raw_path).strip()
        return p[7:] if p.startswith("file://") else p

    def _is_generic_system_icon(self, path_or_name: str) -> bool:
        if not path_or_name:
            return True
        low = path_or_name.lower()
        system_keywords = [
            "call-start", "call-stop", "call-incoming", "call", "phone",
            "telephony", "dialog-information", "notification", "teams-for-linux",
            "com.github.ismaelmartinez", "microsoft-teams", "hicolor"
        ]
        return any(kw in low for kw in system_keywords)

    def _extract_and_save_avatar(self, app_icon: str, hints: Dict[str, Any]) -> Optional[str]:
        try:
            for key in ["image-data", "image_data", "icon_data"]:
                if key in hints and hints[key]:
                    img_struct = hints[key]
                    if len(img_struct) >= 7:
                        width, height, rowstride, has_alpha = img_struct[0], img_struct[1], img_struct[2], bool(img_struct[3])
                        raw_data = bytes(img_struct[6])
                        if width >= 24 and height >= 24:
                            h = hashlib.md5(raw_data[:256]).hexdigest()
                            target_path = os.path.join(self.cache_dir, f"teams_avatar_{h}.png")
                            try:
                                from PIL import Image
                                mode = "RGBA" if has_alpha else "RGB"
                                img = Image.frombytes(mode, (width, height), raw_data, "raw", mode, rowstride)
                                img.save(target_path, "PNG")
                                return target_path
                            except ImportError:
                                pass

            for key in ["image-path", "image_path", "icon_path", "image-uri", "image_uri"]:
                if key in hints and hints[key]:
                    p = self._clean_file_path(hints[key])
                    if os.path.exists(p) and os.path.isfile(p) and not self._is_generic_system_icon(p):
                        return p

            if app_icon:
                p = self._clean_file_path(app_icon)
                if os.path.exists(p) and os.path.isfile(p) and not self._is_generic_system_icon(p):
                    return p
            return None
        except Exception as e:
            self.log.debug(f"提取头像异常: {e}")
            return None

    def _generate_letter_avatar(self, name: str) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFont
            size = (128, 128)
            teams_palette = ["#5C5FC8", "#4F6BED", "#0078D4", "#008272", "#107C41", "#D83B01", "#8764B8", "#B146C2"]
            bg_color = teams_palette[abs(hash(name)) % len(teams_palette)]

            img = Image.new("RGBA", size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([(0, 0), (size[0] - 1, size[1] - 1)], fill=bg_color)

            clean = name.strip()
            text = "T"
            if clean:
                parts = clean.split()
                if len(parts) >= 2 and parts[0][0].isalpha() and parts[1][0].isalpha():
                    text = (parts[0][0] + parts[1][0]).upper()
                else:
                    text = clean[0].upper()

            # 跨发行版字体智能探针
            font = None
            font_size = 52 if len(text) <= 1 else 44
            candidate_fonts = [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"
            ]
            for fp in candidate_fonts:
                if os.path.exists(fp):
                    try:
                        font = ImageFont.truetype(fp, font_size)
                        break
                    except Exception:
                        pass
            if not font:
                font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), text, font=font)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((size[0] - w) / 2 - bbox[0], (size[1] - h) / 2 - bbox[1]), text, fill="white", font=font)

            name_hash = hashlib.md5(name.encode("utf-8")).hexdigest()
            out_path = os.path.join(self.cache_dir, f"teams_letter_{name_hash}.png")
            img.save(out_path, "PNG")
            return out_path
        except Exception:
            return ""