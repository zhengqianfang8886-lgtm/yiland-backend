# ==========================================
# 📄 File: core/state.py
# 🌟 灵动岛全量状态机（包含原生电池与电源配置）
# ==========================================
from typing import Dict, Any, Optional, List


class IslandState:
    def __init__(self):
        # 1. 虚拟桌面
        self.workspace_index: int = 1
        self.workspace_name: str = "Desktop 1"
        self.workspace_total: int = 1
        self.workspaces: List[Dict[str, Any]] = []

        # 2. 媒体播放
        self.active_player: Optional[str] = None
        self.playback_status: str = "Stopped"
        self.title: str = "等待播放媒体..."
        self.artist: str = "空闲"
        self.art_url: str = ""
        self.position: float = 0.0
        self.duration: float = 0.0

        # 3. 音量、亮度与麦克风
        self.volume: int = 50
        self.is_muted: bool = False
        self.brightness: int = 100
        self.mic_volume: int = 80
        self.is_mic_muted: bool = False

        # 4. 电池与电源状态 (原生对接 UPower)
        self.battery_pct: int = 100
        self.battery_charging: bool = False
        self.power_profile: str = "balanced"  # "power-saver" | "balanced" | "performance"

        # 5. 蓝牙模块
        self.bt_powered: bool = True
        self.bt_devices: List[Dict[str, Any]] = []

        # 6. 网络与 VPN
        self.net_connected: bool = False
        self.net_type: str = "none"
        self.net_name: str = "Disconnected"
        self.net_strength: int = 0
        self.net_rx_speed: str = "0 B/s"
        self.net_tx_speed: str = "0 B/s"
        self.vpn_active: bool = False
        self.vpn_name: str = ""
        self.vpn_type: str = ""

        # 7. 浏览器 / Teams 通知模块
        self.last_notify_title: str = ""
        self.last_notify_content: str = ""
        self.last_notify_icon: str = "🌐"
        self.last_notify_avatar: str = ""

    def to_json(self, event_type: str = "FULL_SYNC") -> Dict[str, Any]:
        progress = round(self.position / self.duration, 4) if self.duration > 0 else 0.0

        return {
            "type": event_type,
            "data": {
                "workspace": {
                    "current_index": self.workspace_index,
                    "current_name": self.workspace_name,
                    "total": self.workspace_total,
                    "list": self.workspaces
                },
                "media": {
                    "player": self.active_player,
                    "status": self.playback_status,
                    "title": self.title,
                    "artist": self.artist,
                    "art_url": self.art_url,
                    "position": round(self.position, 1),
                    "duration": round(self.duration, 1),
                    "progress": progress
                },
                "system": {
                    "volume": min(100, max(0, self.volume)),
                    "muted": self.is_muted,
                    "brightness": min(100, max(5, self.brightness)),
                    "mic_volume": min(100, max(0, self.mic_volume)),
                    "mic_muted": self.is_mic_muted,
                    # 原生电池与电源策略广播
                    "battery": {
                        "percentage": self.battery_pct,
                        "charging": self.battery_charging,
                        "profile": self.power_profile
                    }
                },
                "bluetooth": {
                    "powered": self.bt_powered,
                    "connected_devices": self.bt_devices
                },
                "network": {
                    "connected": self.net_connected,
                    "type": self.net_type,
                    "name": self.net_name,
                    "strength": self.net_strength,
                    "rx_speed": self.net_rx_speed,
                    "tx_speed": self.net_tx_speed,
                    "vpn_active": self.vpn_active,
                    "vpn_name": self.vpn_name,
                    "vpn_type": self.vpn_type
                },
                "notification": {
                    "title": self.last_notify_title,
                    "content": self.last_notify_content,
                    "icon": self.last_notify_icon,
                    "avatar": self.last_notify_avatar
                }
            }
        }