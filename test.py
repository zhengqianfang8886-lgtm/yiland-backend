import sys
import shutil
import asyncio
import re

# 颜色高亮
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
RESET = "\033[0m"

async def test_pactl_subscribe():
    """探针 1：系统底层 CLI 原始事件流（绝对基准）"""
    if not shutil.which("pactl"):
        print(f"{RED}[探针 1 失败]{RESET} 系统未安装 pactl 工具")
        return

    cmd = ["pactl", "subscribe"]
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", "pactl", "subscribe"]

    print(f"{CYAN}[探针 1 启动]{RESET} 正在监听 `pactl subscribe` 原始数据...")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode().strip()
            print(f"{GREEN}[探针 1: pactl 原生原始事件]{RESET} -> {text}")
    except Exception as e:
        print(f"{RED}[探针 1 异常]{RESET} {e}")

async def test_pulsectl_asyncio():
    """探针 2：Python pulsectl-asyncio 库事件流"""
    try:
        from pulsectl_asyncio import PulseAsync
    except ImportError:
        print(f"{YELLOW}[探针 2 跳过]{RESET} 未安装 pulsectl-asyncio")
        return

    print(f"{CYAN}[探针 2 启动]{RESET} 正在监听 `pulsectl-asyncio` 套接字...")
    try:
        async with PulseAsync("test-listener") as pulse:
            # 监听所有可能的事件类型
            async for ev in pulse.subscribe_events("all"):
                # 获取该事件在 Python 中的真实类型与属性
                fac = getattr(ev, "facility", "未知")
                evt_type = getattr(ev, "t", "未知")
                idx = getattr(ev, "index", "未知")
                print(
                    f"{YELLOW}[探针 2: pulsectl 库捕获]{RESET} -> "
                    f"facility: {repr(fac)} | type: {repr(evt_type)} | index: {idx}"
                )
    except Exception as e:
        print(f"{RED}[探针 2 异常]{RESET} {e}")

async def test_volume_poller():
    """探针 3：兜底轮询（每 300ms 查一次真实音量，验证按键是否生效）"""
    last_vol = None
    last_mute = None

    while True:
        try:
            if shutil.which("wpctl"):
                proc = await asyncio.create_subprocess_exec(
                    "wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                out, _ = await proc.communicate()
                text = out.decode()
                m = re.search(r"Volume:\s+([0-9.]+)", text)
                cur_vol = int(round(float(m.group(1)) * 100)) if m else -1
                cur_mute = "[MUTED]" in text
            elif shutil.which("pactl"):
                proc = await asyncio.create_subprocess_exec(
                    "pactl", "get-sink-volume", "@DEFAULT_SINK@",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                out, _ = await proc.communicate()
                m = re.search(r"(\d+)%", out.decode())
                cur_vol = int(m.group(1)) if m else -1
                cur_mute = False
            else:
                return

            if last_vol is not None and (cur_vol != last_vol or cur_mute != last_mute):
                print(f"{CYAN}🔥 [探针 3: 硬件音量确已改变]{RESET} 音量: {cur_vol}% | 静音: {cur_mute}")

            last_vol = cur_vol
            last_mute = cur_mute
        except Exception:
            pass

        await asyncio.sleep(0.3)

async def main():
    print("=" * 60)
    print(" 🛠️  Linux 音频硬件按键诊断 Demo 启动")
    print(" 请现在按下你的键盘【音量加/减】或【静音键】，观察终端打印：")
    print("=" * 60)

    await asyncio.gather(
        test_pactl_subscribe(),
        test_pulsectl_asyncio(),
        test_volume_poller(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n测试退出。")
