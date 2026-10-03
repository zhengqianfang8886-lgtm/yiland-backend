import os
import sys
import json
import asyncio
from loguru import logger
from core.logger import setup_logging
from core.backend import DynamicIslandBackend

def format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

async def terminal_cli(backend: DynamicIslandBackend):
    print("\n" + "="*70)
    print(" 🏝️  【灵动岛全功能全集成模块化测试终端】")
    print("="*70)
    print("  [媒体控制]")
    print("    p / pause    : 播放 / 暂停 (PlayPause)")
    print("    n / next     : 下一首 (Next)")
    print("    prev         : 上一首 (Previous)")
    print("    media        : 打印当前音轨详情与秒级进度")
    print("    r            : 重新扫描并连接 MPRIS 播放器")
    print("  [音量与亮度]")
    print("    v+ / v-      : 音量增减 5%")
    print("    vol <0-150>  : 设置绝对音量 (例: vol 80)")
    print("    m            : 切换静音状态")
    print("    b+ / b-      : 亮度增减 10%")
    print("    bri <1-100>  : 设置绝对亮度 (例: bri 70)")
    print("  [虚拟桌面]")
    print("    ws           : 查看当前桌面与所有拓扑工作区")
    print("    ws+ / ws-    : 环形切到 下一个 / 上一个 桌面")
    print("    ws <目标>    : 切换到指定桌面 (支持索引数字或名称, 例: ws 2)")
    print("  [蓝牙 & 网络/VPN]")
    print("    bt           : 查看所有已配对/已连接蓝牙设备及电量")
    print("    bt-power     : 开关蓝牙适配器电源")
    print("    net          : 查看当前网络类型、WiFi强度、VPN及吞吐速率")
    print("  [Teams 消息 & 通话交互]")
    print("    teams        : 查看最新一条 Microsoft Teams 消息/来电信息")
    print("    call         : [测试] 模拟收到 Teams 来电呼叫")
    print("    msg          : [测试] 模拟收到 Teams 普通文字通知")
    print("    f / focus    : [控制] 靶向聚焦并激活 Teams 窗口")
    print("    ans / a      : [控制] 安全接听当前 Teams 来电")
    print("    rej / h      : [控制] 安全拒接/挂断当前 Teams 来电")
    print("  [电源与会话管理]")
    print("    suspend      : 睡眠 (深度低功耗待机)")
    print("    reboot       : 重启 (重新引导系统)")
    print("    poweroff     : 关机 (安全切断电源)")
    print("    lock         : 锁定屏幕 (快捷键 Win+L)")
    print("    logout       : 注销会话 (退出当前账户)")
    print("    switch       : 切换用户 (保留后台任务)")
    print("  [系统管理]")
    print("    dump         : 导出当前灵动岛全量状态 (State JSON)")
    print("    help / ?     : 显示此菜单")
    print("    q            : 退出后台守护服务")
    print("="*70 + "\n")

    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            raw = line.strip()
            if not raw:
                continue

            parts = raw.split()
            cmd = parts[0].lower()
            args = parts[1:]

            # 1. 媒体测试
            if cmd in ('p', 'pause'):
                if backend.mpris: await backend.mpris.control("PlayPause")
            elif cmd in ('n', 'next'):
                if backend.mpris: await backend.mpris.control("Next")
            elif cmd == 'prev':
                if backend.mpris: await backend.mpris.control("Previous")
            elif cmd == 'media':
                s = backend.state
                cur_t = format_time(s.position)
                tot_t = format_time(s.duration)
                pct = round((s.position / s.duration * 100), 1) if s.duration > 0 else 0
                print(f"🎵 媒体曲目 : [{s.playback_status}] '{s.title}' - {s.artist}")
                print(f"⏱️ 进度时间 : {cur_t} / {tot_t} ({pct}%)")
                print(f"🖼️ 专辑封面 : {s.art_url or '无封面'}")
                print(f"🎮 播放源   : {s.active_player or '无活动播放器'}")
            elif cmd == 'r':
                if backend.mpris: await backend.mpris.discover_players()

            # 2. 音量与亮度
            elif cmd == 'v+':
                if backend.audio: await backend.audio.set_delta(5)
            elif cmd == 'v-':
                if backend.audio: await backend.audio.set_delta(-5)
            elif cmd == 'vol' and args:
                try:
                    val = int(args[0])
                    if backend.audio: await backend.audio.set_volume(val)
                except ValueError:
                    print("⚠️ 请输入正确的音量数值 (0-150)。")
            elif cmd == 'm':
                if backend.audio: await backend.audio.toggle_mute()
            elif cmd == 'b+':
                if backend.brightness: await backend.brightness.set_delta(10)
            elif cmd == 'b-':
                if backend.brightness: await backend.brightness.set_delta(-10)
            elif cmd == 'bri' and args:
                try:
                    val = int(args[0])
                    if backend.brightness: await backend.brightness.set_brightness(val)
                except ValueError:
                    print("⚠️ 请输入正确的亮度百分比 (1-100)。")

            # 3. 虚拟桌面测试
            elif cmd == 'ws' and not args:
                s = backend.state
                print(f"🖥️ 当前工作区: [{s.workspace_index}/{s.workspace_total}] '{s.workspace_name}'")
                if s.workspaces:
                    print("📋 桌面拓扑列表:")
                    for w in s.workspaces:
                        tag = "👉 [Active]" if w.get("active") else "   "
                        print(f"  {tag} {w.get('index')}. {w.get('name')} (ID: {w.get('id')})")
            elif cmd in ('ws+', 'ws-'):
                if backend.workspace:
                    await backend.workspace.switch_workspace("next" if cmd == 'ws+' else "prev")
            elif cmd == 'ws' and args:
                target = args[0]
                target = int(target) if target.isdigit() else target
                if backend.workspace:
                    ok = await backend.workspace.switch_workspace(target)
                    print(f"桌面切换结果: {'成功' if ok else '失败'}")

            # 4. 蓝牙与网络
            elif cmd == 'bt':
                s = backend.state
                print(f"📶 蓝牙适配器电源: {'开启' if s.bt_powered else '关闭'}")
                print(f"🎧 已配对/已连接设备 ({len(s.bt_devices)} 个):")
                for d in s.bt_devices:
                    bat = f", 电量: {d['battery']}%" if d['battery'] is not None else ""
                    st = "已连接" if d['connected'] else "未连接"
                    print(f"  - [{st}] {d['name']} (MAC: {d['mac']}{bat})")
            elif cmd == 'bt-power':
                if backend.bluetooth: await backend.bluetooth.toggle_power()
            elif cmd == 'net':
                s = backend.state
                print(f"🌐 网络状态: 连通={s.net_connected}, 模式={s.net_type}, 名称='{s.net_name}', 信号={s.net_strength}%")
                vpn_str = f"🛡️  ON [{s.vpn_name}] ({s.vpn_type})" if s.vpn_active else "OFF"
                print(f"🔒 VPN 状态: {vpn_str}")
                print(f"⚡ 实时速率: ↓ {s.net_rx_speed}  |  ↑ {s.net_tx_speed}")

            # 5. Teams 与通话控制
            elif cmd == 'teams':
                s = backend.state
                status = " [📞 正在响铃呼叫]" if backend.notifications and backend.notifications.is_calling else ""
                print(f"{s.last_notify_icon} Teams 信息: {s.last_notify_title}{status}")
                print(f"   正文: {s.last_notify_content}")
                print(f"   头像缓存: {s.last_notify_avatar or '无'}")
            elif cmd in ('f', 'focus'):
                if backend.notifications:
                    ok = await backend.notifications.focus_teams()
                    print(f"窗口置顶反馈: {'已激活并置顶' if ok else '未找到 Teams 窗口'}")
            elif cmd in ('ans', 'a'):
                if backend.notifications:
                    ok = await backend.notifications.answer_call()
                    print(f"接听执行反馈: {'已接通' if ok else '失败/未处于呼叫态'}")
            elif cmd in ('rej', 'h'):
                if backend.notifications:
                    ok = await backend.notifications.reject_call()
                    print(f"拒接执行反馈: {'已挂断' if ok else '失败/未处于呼叫态'}")
            elif cmd == 'call':
                mod = backend.notifications
                if mod:
                    mod.is_calling = True
                    mod.current_call_id = 999999
                    backend.state.last_notify_title = "Linus Torvalds"
                    backend.state.last_notify_content = "邀请您加入视频通话"
                    backend.state.last_notify_icon = "📞"
                    loop_cur = asyncio.get_running_loop()
                    backend.state.last_notify_avatar = await loop_cur.run_in_executor(None, mod._generate_letter_avatar, "Linus Torvalds")
                    backend.dispatcher.broadcast("TEAMS_CALL_INCOMING")
                    print("📞 [测试] 已触发模拟 Teams 来电！输入 'f' 聚焦置顶、'a' 安全接听、'h' 挂断。")
            elif cmd == 'msg':
                mod = backend.notifications
                if mod:
                    backend.state.last_notify_title = "Satya Nadella"
                    backend.state.last_notify_content = "请确认下季度内核性能指标会议时间"
                    backend.state.last_notify_icon = "👥"
                    loop_cur = asyncio.get_running_loop()
                    backend.state.last_notify_avatar = await loop_cur.run_in_executor(None, mod._generate_letter_avatar, "Satya Nadella")
                    backend.dispatcher.broadcast("TEAMS_NOTIFICATION")
                    print("💬 [测试] 已触发模拟 Teams 消息推送。")

            # 6. 电源与会话管理 (对应图片 6 项)
            elif cmd in ('suspend', 'sleep'):
                if backend.power: await backend.power.suspend()
            elif cmd in ('reboot', 'rb'):
                if backend.power: await backend.power.reboot()
            elif cmd in ('poweroff', 'off', 'shutdown'):
                if backend.power: await backend.power.power_off()
            elif cmd == 'lock':
                if backend.power: await backend.power.lock_screen()
            elif cmd == 'logout':
                if backend.power: await backend.power.logout()
            elif cmd in ('switch', 'switch-user', 'su'):
                if backend.power: await backend.power.switch_user()

            # 7. 系统管理
            elif cmd == 'dump':
                print(json.dumps(backend.state.to_json("DEBUG_DUMP"), indent=2, ensure_ascii=False))
            elif cmd in ('help', '?'):
                await terminal_cli(backend)
                return
            elif cmd == 'q':
                logger.info("正在退出守护进程...")
                await backend.shutdown()
                os._exit(0)

        except Exception as e:
            logger.error(f"CLI 处理异常: {e}")

async def main():
    setup_logging(level="INFO")

    backend = DynamicIslandBackend()
    await backend.initialize()
    await terminal_cli(backend)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("灵动岛服务已通过键盘中断安全退出。")