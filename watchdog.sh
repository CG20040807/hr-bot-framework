#!/bin/bash
# HR Bot 自愈看门狗 v2
#
# 背景：公司 SASE 零信任客户端（Yunshu/亿格云）会偶尔把飞书 WebSocket 长连接
# 劫持进它自己的隧道（fake-IP 198.18.0.0/16、198.19.0.0/16 段），该隧道路径
# 扛不住长连接心跳保活，导致 bot 间歇性"熄火"。这是外部网络安全层的问题，
# 无法从 bot 代码或本机网络配置层面根治，所以做检测 + 自动重启兜底。
#
# v1 教训（2026-07-16 17:00 左右实测发现）：
#   - 每次检查只要发现 0 个 ESTABLISHED 连接就立刻 kickstart，
#     结果在 SASE 正在主动拦截的时间窗口里，每次重启后新进程的
#     握手依然会立刻超时失败——重启对"正在发生的拦截"无效，
#     只会造成 2 分钟一次的重启抖动（重复加载 reranker 模型等），
#     反而可能打断 SDK 自身正常的重连/退避逻辑。
#   - 教训：重启只对"重连逻辑已经彻底卡死/停止重试"这种情况有用
#     （新进程重新 DNS 解析，有机会绕开 SASE 缓存的 fake-ip 隧道），
#     对"当前正在被主动拦截，请求秒超时"这种情况没用，应该让 SDK
#     自己的重试机制继续跑，而不是每 2 分钟打断它一次。
#
# v2 策略：
#   1. 冷却期：距离上次 kickstart 不到 COOLDOWN_SEC，不再重启，
#      给 SDK 自身重连逻辑留出充分时间。
#   2. 连续确认：只有连续 CONSEC_THRESHOLD 次检查都判定"无连接"，
#      才认为是真的卡死了，而不是一次瞬时波动就动手。

LOG="/tmp/hr-bot-watchdog.log"
STATE="/tmp/hr-bot-watchdog.state"
LABEL="com.yourcompany.hr-bot"  # 与 launchd plist 文件名保持一致
COOLDOWN_SEC=1800     # 重启冷却期：30 分钟内不重复重启（避免飞书 429 限流）
CONSEC_THRESHOLD=4    # 连续 4 次（约 20 分钟）判定无连接才动手

now=$(date +%s)
last_kickstart=0
consec_fail=0

# 按用户作息值守（2026-07-27 确认）：工作日约 9-10 点开盖、18-19 点合盖，
# 合盖后机器睡眠、WS 断开是正常状态；节假日不开盖，机器一直睡眠，
# launchd 任务本来就不会被唤醒执行。所以只在工作日 08:00-19:59 值守，
# 其余时间直接退出，避免把"正常睡眠断连"误判成故障而白白重启。
hour=$(date +%H)
dow=$(date +%u)   # 1=周一 … 7=周日
if [ "$dow" -ge 6 ] || [ "$hour" -lt 8 ] || [ "$hour" -ge 20 ]; then
    exit 0
fi

if [ -f "$STATE" ]; then
    # 格式：last_kickstart_epoch consec_fail
    read -r last_kickstart consec_fail < "$STATE"
    last_kickstart=${last_kickstart:-0}
    consec_fail=${consec_fail:-0}
fi

PID=$(pgrep -f "^[^[:space:]]*Python [^[:space:]]*lark-hr-bot-dept/bot\.py")

if [ -z "$PID" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] 进程不存在，触发 kickstart" >> "$LOG"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    echo "$now 0" > "$STATE"
    exit 0
fi

ESTABLISHED_COUNT=$(/usr/sbin/lsof -a -i -P -n -p "$PID" 2>/dev/null | grep -c ESTABLISHED)

if [ "$ESTABLISHED_COUNT" -eq 0 ]; then
    consec_fail=$((consec_fail + 1))
    since_last=$((now - last_kickstart))

    if [ "$consec_fail" -lt "$CONSEC_THRESHOLD" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] PID $PID 无连接（第 $consec_fail 次，未达阈值 $CONSEC_THRESHOLD），先观察不重启" >> "$LOG"
        echo "$last_kickstart $consec_fail" > "$STATE"
    elif [ "$since_last" -lt "$COOLDOWN_SEC" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] PID $PID 持续无连接，但距上次重启仅 ${since_last}s（冷却期 ${COOLDOWN_SEC}s），跳过，让 SDK 自行重试" >> "$LOG"
        echo "$last_kickstart $consec_fail" > "$STATE"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] PID $PID 连续 $consec_fail 次无连接且已过冷却期，触发 kickstart 重启" >> "$LOG"
        launchctl kickstart -k "gui/$(id -u)/$LABEL"
        echo "$now 0" > "$STATE"
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] 正常，PID $PID 有 $ESTABLISHED_COUNT 个活跃连接" >> "$LOG"
    echo "$last_kickstart 0" > "$STATE"
fi
