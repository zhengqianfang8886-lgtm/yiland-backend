import time
from typing import Dict, Any, Optional, List

class IslandState:
    def __init__(self):
        # 虚拟桌面
        self.workspace_index: int = 1
        self.workspace_name: str = "Desktop 1"
        self.workspace_total: int = 1
        self.workspaces: List[Dict[str, Any]] = []

        # 媒体播放
        self.active_player: Optional[str] = None
        self.playback_status: str = "Stopped"
        self.title: str = "等待播放媒体..."
        self.artist: str = "空闲"
        self.art_url: str = ""
        self.position: float = 0.0
        self.duration: float = 0.0

        # 音量与亮度（硬锁定 0~100）
        self.volume: int = 50
        self.is_muted: bool = False
        self.brightness: int = 100

        # 🚀 补全：系统麦克风通道状态
        self.mic_volume: int = 80
        self.is_mic_muted: bool = False

        # 蓝牙模块
        self.bt_powered: bool = True
        self.bt_devices: List[Dict[str, Any]] = []

        # 网络与 VPN
        self.net_connected: bool = False
        self.net_type: str = "none"
        self.net_name: str = "Disconnected"
        self.net_strength: int = 0
        self.net_rx_speed: str = "0 B/s"
        self.net_tx_speed: str = "0 B/s"
        self.vpn_active: bool = False
        self.vpn_name: str = ""
        self.vpn_type: str = ""

        # 浏览器 / Teams 通知模块
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
                    # 🚀 同步麦克风状态给灵动岛前端
                    "mic_volume": min(100, max(0, self.mic_volume)),
                    "mic_muted": self.is_mic_muted
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